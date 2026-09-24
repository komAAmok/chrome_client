"""Resolves the Chromium checkout for the Python tooling.

Mirrors tools/core-paths.sh so a Python audit and a shell audit agree on where
the checkout is. Precedence: $CHROMIUM_SRC, then a sibling checkout
(<parent-of-repo>/chromium/src), then $HOME/chromium/src.
"""

from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def chromium_src() -> pathlib.Path:
    override = os.environ.get("CHROMIUM_SRC")
    if override:
        return pathlib.Path(override)
    sibling = ROOT.parent / "chromium" / "src"
    if sibling.is_dir():
        return sibling
    home = pathlib.Path(os.environ.get("HOME", "")) / "chromium" / "src"
    if home.is_dir():
        return home
    return sibling
