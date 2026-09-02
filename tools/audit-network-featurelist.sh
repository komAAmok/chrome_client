#!/usr/bin/env bash
set -Eeuo pipefail

OUT_DIR=${1:?usage: audit-network-featurelist.sh OUT_DIR}
CHROMIUM_SRC=${CHROMIUM_SRC:-/home/sj/chromium/src}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

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

sources = []
for line in ninja.read_text().splitlines():
    match = re.match(
        r"build obj/net/\S+\.o: \S+ ../../(net/\S+\.(?:cc|c|mm))(?: |$)",
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
expected_digest = "37169aa7dfe3e211c9d817060dfcc16bc500e750b8f4bf4308827aea0e221f3d"
if len(sources) != 529 or len(entries) != 166 or digest != expected_digest:
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
obj = out_dir / "obj/net/net/socket_pool_additional_capacity.o"
nm = chromium / "third_party/llvm-build/Release+Asserts/bin/llvm-nm"
if not obj.is_file():
    raise SystemExit(f"missing audited object: {obj}")
undefined = subprocess.run(
    [nm, "-uC", obj], check=True, capture_output=True, text=True
).stdout
if "base::FeatureList::IsEnabled" in undefined:
    raise SystemExit("socket-pool object still reads the process FeatureList")

print(
    f"Audited {len(sources)} release net sources and {len(entries)} frozen "
    f"global feature reads ({digest[:12]})."
)
PY
