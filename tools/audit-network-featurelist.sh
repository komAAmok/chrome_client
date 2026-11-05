#!/usr/bin/env bash
set -Eeuo pipefail

OUT_DIR=${1:?usage: audit-network-featurelist.sh OUT_DIR}
# The Chromium checkout defaults to a sibling of this repository, so this
# script works from any directory on any machine. Point CHROMIUM_SRC at your
# checkout if it lives elsewhere.
source "$(dirname "${BASH_SOURCE[0]}")/core-paths.sh"
CHROMIUM_SRC=$(resolve_chromium_src)
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

test -d "$CHROMIUM_SRC/net" || { printf 'audit-network-featurelist: no Chromium checkout at %s; set CHROMIUM_SRC to your Chromium checkout\n' "$CHROMIUM_SRC" >&2; exit 1; }

python3 - "$CHROMIUM_SRC" "$OUT_DIR" "$ROOT_DIR" <<'PY'
import hashlib
import re
import subprocess
import sys
from pathlib import Path

chromium, out_dir, root = map(Path, sys.argv[1:])
ninja = out_dir / "obj/net/net.ninja"
if not ninja.is_file():
    raise SystemExit(f"missing release net graph: {ninja}")

# The object suffix is the platform's: Linux/macOS emit `.o`, Windows emits
# `.obj`. Hard-coding `.o` made this scan match nothing on Windows, and the
# "unknown release-graph" error then blamed the source tree rather than the
# miss. Both spellings are accepted here.
sources = []
for line in ninja.read_text().splitlines():
    match = re.match(
        r"build obj/net/\S+\.(?:o|obj): \S+ ../../(net/\S+\.(?:cc|c|mm))(?: |$)",
        line,
    )
    if match:
        sources.append(chromium / match.group(1))


def calls(text, marker):
    position = 0
    while True:
        start = text.find(marker, position)
        if start < 0:
            return
        opening = start + len(marker)
        while opening < len(text) and text[opening].isspace():
            opening += 1
        if opening == len(text) or text[opening] != "(":
            position = opening
            continue
        depth = 0
        closing = opening
        while closing < len(text):
            if text[closing] == "(":
                depth += 1
            elif text[closing] == ")":
                depth -= 1
                if depth == 0:
                    break
            closing += 1
        if depth:
            raise SystemExit(f"unterminated {marker} call")
        yield " ".join(text[opening + 1 : closing].split())
        position = closing + 1


entries = set()
for source in sources:
    text = source.read_text(errors="replace")
    relative = str(source.relative_to(chromium))
    for argument in calls(text, "FeatureList::IsEnabled"):
        match = re.search(
            r"(?:(?:net|base)::)?features::(k\w+)|base::(k\w+)|\b(k\w+)",
            argument,
        )
        symbol = next((value for value in match.groups() if value), argument) \
            if match else argument
        entries.add((relative, "feature", symbol))
    for match in re.finditer(
        r"((?:(?:net|base)::)?features::k\w+|\bk[A-Z]\w*)\.Get\s*\(\s*\)",
        text,
    ):
        entries.add((relative, "param", match.group(1)))
    for marker in (
        "GetFieldTrialParamByFeatureAsInt",
        "GetFieldTrialParamValueByFeature",
        "GetFieldTrialParamValue",
    ):
        for argument in calls(text, marker):
            entries.add(
                (relative, "field_trial", f"{marker}:{argument.split(',')[0]}")
            )
    for match in re.finditer(r"FieldTrialList::(\w+)", text):
        entries.add(
            (relative, "field_trial", f"FieldTrialList::{match.group(1)}")
        )

inventory = "".join("\t".join(entry) + "\n" for entry in sorted(entries))
digest = hashlib.sha256(inventory.encode()).hexdigest()

# net/BUILD.gn selects platform sources, so each platform family compiles a
# different set and freezes a different number of reads. A single baseline
# could only ever be correct for one of them: pinning Linux's 534/175 made the
# other families unverifiable, and the failure looked like source-tree drift
# rather than "this platform was never baselined". The three families below are
# exactly what their committed artifacts are built from; adding a platform means
# measuring it once and adding a row here.
BASELINES = {
    "linux": (534, 175, "e93647bc802ff3bffcb0e364eb4e7fdd1389a910b3e1b860b8daf34c9ba259ad"),
    "win":   (530, 180, "8069cc09714029d9ca9da23ad0be3287ca053d2fa77ed5281b835580261c08ae"),
    "macos": (542, 178, "12b48c7ef887233c591e19cee4de82ee18aad7afbad8b7b6ce10b4fa780b9188"),
}
family = next((name for name in BASELINES if name in out_dir.name.lower()), None)
if family is None:
    raise SystemExit(
        "audit-network-featurelist: cannot tell which platform baseline to use from "
        f"{out_dir.name}; expected linux/win/macos in the build directory name"
    )
expected_sources, expected_reads, expected_digest = BASELINES[family]

if (
    len(sources) != expected_sources
    or len(entries) != expected_reads
    or digest != expected_digest
):
    print(inventory, file=sys.stderr, end="")
    raise SystemExit(
        "unknown release-graph FeatureList/FeatureParam/field-trial read: "
        f"sources={len(sources)} reads={len(entries)} sha256={digest}"
    )

core_reads = []
# Core sources live under core/source/ with headers in core/source/minicronet/.
for source in sorted((root / "core/source").rglob("*.[ch]*")):
    text = source.read_text(errors="replace")
    for match in re.finditer(r"FeatureList::(\w+)", text):
        core_reads.append((source.name, match.group(1)))
if core_reads != [("engine.cc", "GetInstance"), ("engine.cc", "InitInstance")]:
    raise SystemExit(f"unexpected MiniCronet-owned FeatureList access: {core_reads}")

ssl_source = (chromium / "net/socket/ssl_client_socket_impl.cc").read_text()
if "const bool has_profile =" not in ssl_source or re.search(
    r"profile_(?:grease_signature_algorithms|use_new_alps_codepoint)\s*&&\s*"
    r"base::FeatureList::IsEnabled",
    ssl_source,
):
    raise SystemExit("TLS profile remains coupled to process FeatureList")

capacity_source = (
    chromium / "net/socket/socket_pool_additional_capacity.cc"
).read_text()
for marker in (
    "#if BUILDFLAG(MINICRONET_BUILD)",
    "SocketPoolAdditionalCapacity(0.000001, additional_capacity, 0.01,",
):
    if marker not in capacity_source:
        raise SystemExit("socket-pool profile randomization is not frozen")

# Prove the MiniCronet preprocessor branch removed the global feature lookup.
# Same suffix problem as the source scan above: Windows names this .obj.
obj = out_dir / "obj/net/net/socket_pool_additional_capacity.o"
if not obj.is_file():
    obj = out_dir / "obj/net/net/socket_pool_additional_capacity.obj"
nm = chromium / "third_party/llvm-build/Release+Asserts/bin/llvm-nm"
if not obj.is_file():
    raise SystemExit(f"missing audited object: {obj}")
undefined = subprocess.run(
    [nm, "-uC", obj], check=True, capture_output=True, text=True
).stdout
if "base::FeatureList::IsEnabled" in undefined:
    raise SystemExit("socket-pool object still reads the process FeatureList")

print(
    f"Audited {len(sources)} release net sources ({family}) and {len(entries)} "
    f"frozen global feature reads ({digest[:12]})."
)
PY
