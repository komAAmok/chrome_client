#!/usr/bin/env python3
"""Type-checks the stubs the way CI does.

The stubs are hand-written, so mypy is the only thing that proves a name
referred to in a ``.pyi`` is actually imported and that a caller sees the type
the stub promises. CI ran it, but nothing in the local gate list did, so a
missing ``Tuple``/``Literal`` import reached the remote and failed the build
there -- the slowest possible place to find out.

Runs the two commands from .github/workflows/ci.yml verbatim.

    python3 tools/audit-python-typing.py            # needs mypy; fails if absent
    python3 tools/audit-python-typing.py --optional # SKIPPED, exit 0, if absent

mypy is a type-checking-only dependency, and this script is in the AGENTS.md
gate list, so it *is* a gate: a gate that cannot run must not report success.
It used to print "mypy is not installed; skipping." and exit 0, which is the
"a local gate you cannot run is no gate" failure mode. The check was already
finding real defects -- two undefined ``Dict`` names in sessions.pyi -- and no
local run ever showed them, because every local run said "skipped" and CI was
the only place it actually executed.

The default is therefore to fail when mypy is missing. `--optional` is the
documented escape hatch for a checkout that never intended to type-check; it
prints SKIPPED with the reason, so a caller reading logs cannot mistake it for a
pass. Neither the AGENTS.md gate list nor .github/workflows/ci.yml passes it.

Exit status 0 when clean, 1 when mypy reports an error, and 1 when mypy is
missing without `--optional`.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "tools"))

from python_exe import interpreter  # noqa: E402  (needs ROOT above)
PACKAGE = ROOT / "bindings" / "python" / "chrome_client"
CONSUMER = ROOT / "tools" / "typecheck-consumer.py"
MYPYPATH = ROOT / "bindings" / "python"


def have_mypy() -> bool:
    return importlib.util.find_spec("mypy") is not None


def run(label: str, argv: list[str], env: dict[str, str]) -> bool:
    print("== %s" % label)
    result = subprocess.run([interpreter(), "-m", "mypy"] + argv,
                            cwd=str(ROOT), env=env)
    return result.returncode == 0


def main(argv: list[str]) -> int:
    optional = "--optional" in argv
    unknown = [arg for arg in argv if arg != "--optional"]
    if unknown:
        print("usage: %s [--optional]" % pathlib.Path(__file__).name, file=sys.stderr)
        return 1

    if not have_mypy():
        install = "python3 -m pip install 'mypy>=1.11,<2' typing_extensions"
        if optional:
            # Explicitly marked, never silent: the word SKIPPED and the reason are
            # both required so this cannot be read as a passing type check.
            print("audit-python-typing: SKIPPED -- mypy is not installed (%s)" % install)
            return 0
        print(
            "audit-python-typing: FAILED -- mypy is not installed, so the stubs "
            "were not type-checked.\n"
            "  install it with: %s\n"
            "  (pass --optional only if this checkout never intended to type-check; "
            "the AGENTS.md gate list and CI do not)" % install,
            file=sys.stderr,
        )
        return 1

    env = dict(os.environ)
    env["MYPYPATH"] = str(MYPYPATH)

    # 1. The stubs must be internally consistent: every name they refer to has
    #    to be imported, or a checker silently degrades the type to Any.
    stubs_ok = run("stubs", [str(PACKAGE), "--python-version", "3.10",
                             "--ignore-missing-imports"], env)

    # 2. A consumer must see what the stub promises. --warn-unused-ignores is
    #    what turns the fixture's two deliberate mistakes into assertions.
    consumer_ok = run("consumer", [str(CONSUMER), "--python-version", "3.10",
                                   "--ignore-missing-imports",
                                   "--warn-unused-ignores"], env)

    if stubs_ok and consumer_ok:
        print("Python typing audit passed: stubs and consumer both clean.")
        return 0
    print("Python typing audit FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
