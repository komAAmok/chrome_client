#!/usr/bin/env python3
"""The interpreter to spawn child processes with.

Six tools used ``sys.executable`` directly to re-enter Python for a child
process. That attribute is empty when the interpreter was started through a
wrapper that did not exec the real binary, and ``subprocess.run([""])`` is a
PermissionError rather than a useful message: measured on this host,
"PermissionError: [Errno 13] Permission denied: ''" from
``tools/leak-probe.py`` and ``tools/audit-python36-artifact.py``, which made
both gates unrunnable instead of reporting anything about the artifact.

``tools/leak-hunt.py`` and ``tools/prof-attr.py`` had each grown their own
copy of this resolution. Two copies were not enough: the other six call sites
kept the bare attribute. This module is that resolution, in one place, so the
next tool cannot reintroduce the failure.
"""

from __future__ import annotations

import shutil
import sys


def interpreter() -> str:
    """A program name that subprocess can always execute.

    Order: ``sys.executable`` when it is a non-empty path, then ``python3``
    on PATH, then the literal ``"python3"`` so a caller without PATH still
    gets a normal FileNotFoundError naming the program instead of a
    PermissionError naming an empty string.

    Returns:
        The interpreter to spawn.
    """
    if sys.executable:
        return sys.executable
    return shutil.which("python3") or "python3"
