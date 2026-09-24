#!/usr/bin/env python3
"""Type-checks the stubs the way CI does.

The stubs are hand-written, so mypy is the only thing that proves a name
referred to in a ``.pyi`` is actually imported and that a caller sees the type
the stub promises. CI ran it, but nothing in the local gate list did, so a
missing ``Tuple``/``Literal`` import reached the remote and failed the build
there -- the slowest possible place to find out.

Runs the two commands from .github/workflows/ci.yml verbatim. Needs mypy, which
is a type-checking-only dependency: when it is absent this script says so and
exits 0 rather than failing a checkout that never intended to type-check.

    python3 tools/audit-python-typing.py

Exit status 0 when clean, 1 when mypy reports an error.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "bindings" / "python" / "chrome_client"
CONSUMER = ROOT / "tools" / "typecheck-consumer.py"
MYPYPATH = ROOT / "bindings" / "python"


def have_mypy() -> bool:
    return importlib.util.find_spec("mypy") is not None


def run(label: str, argv: list[str], env: dict[str, str]) -> bool:
    print("== %s" % label)
    result = subprocess.run([sys.executable, "-m", "mypy"] + argv,
                            cwd=str(ROOT), env=env)
    return result.returncode == 0


def main() -> int:
    if not have_mypy():
        print("audit-python-typing: mypy is not installed; skipping.")
        print("  install it with: python3 -m pip install 'mypy>=1.11,<2' typing_extensions")
        return 0

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
    sys.exit(main())
