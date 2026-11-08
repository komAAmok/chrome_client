#!/usr/bin/env python3
"""Asserts README.en.md's tables still say the same thing as README.md's.

The two files are the same document in two languages, and nothing compared them:
`audit-readme.sh` checks that bindings/python36/README.md is a byte mirror of
README.md, that both pyprojects point at a readme, and that every link target is
absolute -- but README.en.md was only ever opened to scan its links. So when a
platform was added to one capability table and not the other, the gate stayed
green and the two documents described different products.

That happened: commit 0172856 added ARMv7 to the Chinese table only. The English
one went on saying Linux is "x86, x86_64, ARM64", and the Chinese one claimed a
wheel for a platform that has none.

A translation cannot be compared line for line, so the comparison is on the two
things that must survive translation:

  1. the number of tables, and each table's row and column count -- a row added
     to one language and not the other is exactly how the ARMv7 drift looked;
  2. the set of architecture tokens inside each table -- cells like "ARMv7
     (hard-float, NEON)" are prose, but the architecture names themselves are
     the same word in Chinese and English.

Deliberately *not* compared: the `code` spans inside cells. They are prose-adjacent
and legitimately differ -- `#片段` becomes `#fragment`, and a document reference can
be added to one language first (`docs/CHROME_86_98_SUPPORT.md` is cited in the
Chinese profile table and not the English one). Failing on those would make the gate
noisy enough to be disabled, which is worse than the drift it is here to catch.

    python3 tools/audit-readme-tables.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = (ROOT / "README.md", ROOT / "README.en.md")

#: Architecture names as they appear inside table cells. Deliberately a closed
#: vocabulary rather than "any latin word": the point is to notice that one file
#: names an architecture the other does not, and a permissive pattern would match
#: ordinary prose in both and prove nothing.
ARCH_RE = re.compile(
    r"\b(armv[0-9]+[a-z]*|aarch64|arm64|armhf|x86_64|x86-64|i[3-6]86|x86|amd64)\b",
    re.IGNORECASE,
)
#: A backticked span. Identifiers never translate, so these must match exactly.
CODE_RE = re.compile(r"`([^`]+)`")
SEPARATOR_RE = re.compile(r"^\|?[\s:|-]+\|?$")


def split_cells(line):
    """Splits a table row on unescaped pipes, dropping the outer ones."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells = []
    current = []
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def tables_of(text):
    """Returns every table as (header cells, data rows) in document order."""
    tables = []
    header = None
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        is_row = stripped.startswith("|") and stripped.count("|") >= 2
        if not is_row:
            if header is not None:
                tables.append((header, rows))
            header, rows = None, []
            continue
        if header is None:
            header = split_cells(stripped)
            continue
        if SEPARATOR_RE.match(stripped) and "-" in stripped:
            continue
        rows.append(split_cells(stripped))
    if header is not None:
        tables.append((header, rows))
    return tables


def arch_tokens(cells):
    found = set()
    for cell in cells:
        # Drop code spans first: `armv7-unknown-linux-gnueabihf` is a triple,
        # not an architecture claim about a row, and its substrings would
        # otherwise add armv7 and aarch64 noise to every file equally.
        plain = CODE_RE.sub(" ", cell)
        for match in ARCH_RE.finditer(plain):
            token = match.group(1).lower()
            if token == "x86-64":
                token = "x86_64"
            if token in ("aarch64", "arm64"):
                token = "arm64"
            found.add(token)
    return found


def describe(index):
    return "table %d" % (index + 1)


def compare():
    documents = []
    for path in SOURCES:
        documents.append((path.name, tables_of(path.read_text(encoding="utf-8"))))

    (left_name, left), (right_name, right) = documents
    failures = []

    if len(left) != len(right):
        failures.append("%s has %d tables but %s has %d"
                        % (left_name, len(left), right_name, len(right)))
        return failures

    for index, ((left_header, left_rows), (right_header, right_rows)) in enumerate(
            zip(left, right)):
        label = describe(index)
        if len(left_header) != len(right_header):
            failures.append("%s: %d columns in %s, %d in %s"
                            % (label, len(left_header), left_name,
                               len(right_header), right_name))
            continue
        if len(left_rows) != len(right_rows):
            failures.append(
                "%s: %d data rows in %s, %d in %s; a row that exists in one "
                "language and not the other is how the ARMv7 drift started"
                % (label, len(left_rows), left_name, len(right_rows), right_name))
            continue
        for row_index, (left_row, right_row) in enumerate(
                zip(left_rows, right_rows)):
            if len(left_row) != len(right_row):
                failures.append("%s row %d: %d cells in %s, %d in %s"
                                % (label, row_index + 1, len(left_row), left_name,
                                   len(right_row), right_name))
                continue

            left_arch = arch_tokens(left_row)
            right_arch = arch_tokens(right_row)
            if left_arch != right_arch:
                failures.append(
                    "%s row %d: architectures differ; only in %s: %s; only in "
                    "%s: %s"
                    % (label, row_index + 1, left_name,
                       sorted(left_arch - right_arch) or "none", right_name,
                       sorted(right_arch - left_arch) or "none"))
    return failures


def main():
    for path in SOURCES:
        if not path.is_file():
            print("audit-readme-tables: missing %s" % path.name, file=sys.stderr)
            return 1
    failures = compare()
    if failures:
        print("README table audit FAILED:", file=sys.stderr)
        for failure in failures:
            print("  " + failure, file=sys.stderr)
        return 1
    tables = len(tables_of(SOURCES[0].read_text(encoding="utf-8")))
    print("README table audit passed: %d tables agree between README.md and "
          "README.en.md on row count, column count and architecture names" % tables)
    return 0


if __name__ == "__main__":
    sys.exit(main())
