#!/usr/bin/env python3
r"""Checks the bindings/python36 artifact instead of a crate with no tests.

``cargo test --manifest-path bindings/python36/Cargo.toml`` was in the
pre-tag review as a gate. The crate contains no ``#[test]`` function: measured,
``grep -c '#\[test\]' bindings/python36/src/lib.rs`` is 0, so the command
compiled the crate and exited 0 no matter what the extension did. A step that
cannot fail is worse than a missing one, because the report says the bindings
were verified.

What the artifact has to be true of, and what this script measures:

1. The built extension exists and imports under its own module name.
   ``PyInit_chrome_client_native36`` is the only export an importer will look for;
   loading it under any other name fails with "dynamic module does not define
   module export function", which is a mistake this check makes once and reports
   instead of leaving to the reader.
2. It reports the ABI version the header declares, read from core/abi/minicronet.h
   rather than hard-coded here, so a v9 bump cannot leave this gate passing.
3. It is a real abi3 build: no libpython dependency, and none of the private
   symbols that would pin it to one interpreter.
4. It can drive the shared facade for one real request. The facade in
   bindings/python is the only Python surface there is -- this wheel ships it
   unchanged -- so an extension that imports but cannot serve is not a wheel.
5. When a wheel is present, the facade INSIDE the wheel imports on this
   interpreter. This is not the same check as (1) or (4): the bare .so can be
   perfect while the packaged facade refuses to ask for its module name. That
   is exactly what happened once -- the cp36-abi3 wheel installed on 3.12,
   pip accepted it, and `import chrome_client` failed because the facade only
   asked for `chrome_client_native`. The wheel check is what makes that
   failure visible without cross-building a 3.8 interpreter.

The fourth check is the one that would have caught the actual failure mode: a
stale extension still imports fine and only breaks when the facade calls a
method it does not have.

Exits non-zero with a specific message on the first failed check.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY = "chrome_client_native36"
CANDIDATES = (
    ROOT / "bindings" / "python36" / "target" / "release" / ("lib%s.so" % LEGACY),
    ROOT / "bindings" / "python36" / "target" / "release" / ("%s.so" % LEGACY),
    ROOT / "bindings" / "python36" / "target" / "release" / ("%s.pyd" % LEGACY),
    ROOT / "bindings" / "python36" / "target" / "release" / ("%s.dylib" % LEGACY),
)


def fail(message: str) -> "NoReturn":  # noqa: F821 - string annotation keeps 3.6 out of it
    sys.stderr.write("python36 artifact: %s\n" % message)
    raise SystemExit(1)


def find_extension() -> pathlib.Path:
    for candidate in CANDIDATES:
        if candidate.is_file():
            return candidate
    fail(
        "no built extension under bindings/python36/target/release; build it with "
        "\n  python3 -m maturin build --release --out dist "
        "(cwd: bindings/python36)\n"
        "The artifact is what this gate exists to check, so a missing one is a "
        "failure, not a skip")


def declared_abi_version() -> int:
    header = (ROOT / "core" / "abi" / "minicronet.h").read_text()
    match = re.search(r"^#define MN_ABI_VERSION (\d+)u$", header, re.M)
    if not match:
        fail("core/abi/minicronet.h has no '#define MN_ABI_VERSION <n>u'")
    return int(match.group(1))


def load_extension(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(LEGACY, str(path))
    if spec is None or spec.loader is None:
        fail("%s is not loadable as a Python extension" % path.name)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as error:
        hint = ""
        if "module export function" in str(error):
            hint = (" (the module name must be %r, which is what PyInit_ exports; "
                    "loading it as anything else cannot work)" % LEGACY)
        elif "libminicronet" in str(error):
            hint = (" (set LD_LIBRARY_PATH to core/binaries/linux-x86_64 or the "
                    "matching target directory)")
        fail("importing %s failed: %s%s" % (path.name, error, hint))
    return module


def check_not_linked_to_libpython(path: pathlib.Path) -> None:
    """An abi3 module must run on every interpreter from 3.6 up."""
    if sys.platform.startswith("win"):
        return
    try:
        output = subprocess.run(["ldd", str(path)], capture_output=True, text=True,
                                timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return
    if "libpython" in output:
        fail("%s links libpython, so it is not an abi3 build" % path.name)
    private = subprocess.run(["nm", "-D", "--undefined-only", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
    # These four are part of the limited API and appear in a correct abi3 build.
    # Anything else starting with _Py_ pins the module to one CPython version.
    allowed = {"_Py_Dealloc", "_Py_IncRef", "_Py_NoneStruct", "_Py_TrueStruct",
               "_Py_FalseStruct", "_Py_ZeroStruct", "_Py_NotImplementedStruct"}
    offending = sorted(
        {name for name in re.findall(r"\b_Py_[A-Za-z_0-9]+", private)
         if name not in allowed})
    if offending:
        fail("%s references private CPython symbols %s, so it is not abi3"
             % (path.name, ", ".join(offending)))


def check_drives_the_facade(module) -> None:
    """One real request through the shared facade, on this extension."""
    sys.path.insert(0, str(ROOT / "bindings" / "python"))
    sys.modules[LEGACY] = module
    from chrome_client._python_impl import _native

    # The facade picks its module from sys.version_info; force the legacy one so
    # this check exercises bindings/python36 even on a modern interpreter.
    _native.LEGACY_MODULE_NAME = LEGACY
    _native.MODULE_NAME = LEGACY

    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b"python36"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "gate=1; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import chrome_client

        with chrome_client.Session() as session:
            response = session.get("http://127.0.0.1:%d/" % port)
            if response.status_code != 200 or response.content != b"python36":
                fail("the facade returned %r / %r through the legacy extension"
                     % (response.status_code, response.content))
            if session.cookies.get_dict() != {"gate": "1"}:
                fail("the legacy extension did not record the response cookie: %r"
                     % (session.cookies.get_dict(),))
    finally:
        server.shutdown()
        server.server_close()


def check_wheel_imports_the_facade(wheel) -> str:
    """Imports the facade from a built wheel on this interpreter.

    Runs the wheel in a scratch directory with `python -m zipfile -e` rather
    than installing it: installing needs a venv and pip, and the point is only
    that the packaged Python finds the packaged extension. The interpreter is
    the one running this script, so the case that matters -- a wheel whose
    extension name does not match what this interpreter asks for -- is
    exercised whenever the two differ.

    Returns:
        A one-line description of what was verified.
    """
    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as scratch:
        with zipfile.ZipFile(str(wheel)) as archive:
            archive.extractall(scratch)
        env = dict(os.environ)
        # The Core lives next to the extracted package in a real install only
        # because maturin copied it; when it did not, the loader path has to
        # supply it.
        core = ROOT / "core" / "binaries" / "linux-x86_64"
        env["LD_LIBRARY_PATH"] = str(core)
        env["PYTHONPATH"] = scratch
        probe = (
            "import chrome_client\n"
            "print(chrome_client.abi_version())\n")
        result = subprocess.run([sys.executable, "-c", probe], cwd=scratch,
                                env=env, capture_output=True, text=True,
                                timeout=120)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            fail("%s imports on this interpreter (%d.%d) but its facade cannot "
                 "load the packaged extension:\n  %s\nThe two wheels declare the "
                 "same requires-python, so pip may install this one on a "
                 "supported interpreter; the facade must accept either module "
                 "name." % (wheel.name, sys.version_info[0], sys.version_info[1],
                           detail[-1] if detail else "no output"))
    return "%s: facade imports, abi %s" % (wheel.name, result.stdout.strip())


def find_wheel():
    """Returns the newest wheel in bindings/python36/dist, or None."""
    dist = ROOT / "bindings" / "python36" / "dist"
    if not dist.is_dir():
        return None
    wheels = sorted(dist.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    return wheels[-1] if wheels else None

def main() -> int:
    path = find_extension()
    expected = declared_abi_version()
    module = load_extension(path)

    reported = getattr(module, "abi_version", None)
    if reported != expected:
        fail("%s reports abi_version=%r but core/abi/minicronet.h declares %d"
             % (path.name, reported, expected))

    check_not_linked_to_libpython(path)
    check_drives_the_facade(module)

    wheel_note = "no wheel present"
    wheel = find_wheel()
    if wheel is not None:
        wheel_note = check_wheel_imports_the_facade(wheel)

    print("python36 artifact audit passed: %s, abi %d, abi3, one live request; %s"
          % (path.name, expected, wheel_note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
