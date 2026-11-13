#!/usr/bin/env python3
"""Runs every doctest in the Python facade.

The facade's docstrings are the only place a claim is written twice: once as
prose and once as an example that is supposed to be true. Nothing executed
them, and that is not hypothetical. abi_version() documented

    The ABI number, 8 for this release.
    >>> abi_version()
    8

for two releases after core/abi/minicronet.h had moved to 10. The same text had
been copied into the hand-written stub and into generate-python-stubs.py's
template, so the wrong number was published in three files and nothing compared
it to anything.

Running the examples is the check. An example that cannot run honestly -- one
that would build a Chromium engine, or reach the network -- is marked
"# doctest: +SKIP" with the reason in the surrounding prose, rather than left
unexecuted by accident.

    tools/audit-doctests.py            # exit 1 on any failure
    tools/audit-doctests.py --verbose  # print every example

Exit code is 1 when any example fails, and also when the facade could not be
imported at all: "no examples ran" and "every example passed" must not look the
same.
"""

from __future__ import annotations

import argparse
import doctest
import importlib
import pathlib
import pkgutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bindings" / "python"))

#: Only _python_impl is scanned: the top-level chrome_client and
#: chrome_client.requests modules are aliases into it, so scanning them would
#: count every example three times.
PACKAGE = "chrome_client._python_impl"


def load_package():
    """Imports the facade package, or explains why it could not be loaded.

    The extension and the Core are needed: importing the facade reaches the
    native module. A missing Core is reported as unavailable rather than as a
    pass.

    Returns:
        (package, None) on success, (None, reason) otherwise.
    """
    try:
        return importlib.import_module(PACKAGE), None
    except Exception as error:  # the reason is the output
        return None, "%s: %s" % (type(error).__name__, error)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="print every example")
    args = parser.parse_args(argv)

    package, failure = load_package()
    if package is None:
        print("audit-doctests: the facade could not be imported, so no example "
              "ran; this is not a pass:\n  %s" % failure, file=sys.stderr)
        return 1

    modules = [package]
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        modules.append(importlib.import_module(info.name))

    total_failed = 0
    total_tried = 0
    checked = 0
    finder = doctest.DocTestFinder()
    for module in modules:
        # A module may legitimately contain no examples; only modules that do
        # are reported, so the output is a list of what was actually checked.
        if not sum(len(test.examples) for test in finder.find(module)):
            continue
        checked += 1
        failed, tried = doctest.testmod(module, verbose=args.verbose)
        total_failed += failed
        total_tried += tried
        print("%-4s %-24s %d examples"
              % ("ok" if not failed else "FAIL", module.__name__.rsplit(".", 1)[-1], tried))

    if total_failed:
        print("audit-doctests: %d of %d examples failed" % (total_failed, total_tried),
              file=sys.stderr)
        return 1
    print("doctest audit passed: %d examples in %d modules, none failed"
          % (total_tried, checked))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
