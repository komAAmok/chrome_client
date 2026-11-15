#!/bin/bash
# Mutation tests for the fingerprint gates added with the discard fix.
#
# AGENTS section 6: a check that has not been broken on purpose is not a check.
# Each case copies the tree to a scratch directory, breaks exactly one thing, and
# asserts the gate exits non-zero. Restoring is unnecessary because the scratch
# copy is thrown away.
set -u
cd "$(dirname "$0")/.."
REPO=$PWD
FAILED=0

check() {
  local name="$1"; shift
  if "$@" > /dev/null 2>&1; then
    echo "MUTATION NOT DETECTED: $name"
    FAILED=1
  else
    echo "ok: $name"
  fi
}

scratch() {
  rm -rf "$1" && mkdir -p "$1/bindings" "$1/tools"
  cp -r "$REPO/bindings/python" "$1/bindings/"
  cp -r "$REPO/profiles" "$1/"
  cp "$REPO"/tools/audit-*.py "$1/tools/"
  ln -sfn "$REPO/target" "$1/target"
}

run_gate() {
  local tree="$1" gate="$2"
  ( cd "$tree" && LD_LIBRARY_PATH="$REPO/core/binaries/linux-x86_64" \
      PYTHONPATH=bindings/python python3 "tools/$gate" )
}

# 1. A default header value drifts: the pinned baseline must notice.
scratch /tmp/mut-hdr-value
python3 - <<PY
import pathlib
for name in ("client_hints.py", "browser_headers.py"):
    path = pathlib.Path("/tmp/mut-hdr-value/bindings/python/chrome_client/_python_impl/" + name)
    text = path.read_text()
    path.write_text(text.replace("zh-CN,zh;q=0.9", "en-US,en;q=0.5"))
PY
check "frozen-headers: a default field value changed" \
  run_gate /tmp/mut-hdr-value audit-frozen-headers.py

# 2. The field ORDER changes with the set intact.
scratch /tmp/mut-hdr-order
python3 - <<PY
import pathlib
p = pathlib.Path("/tmp/mut-hdr-order/bindings/python/chrome_client/_python_impl/browser_headers.py")
s = p.read_text()
old = "    return [name for name, _value in browser_headers(profile)]"
assert old in s
p.write_text(s.replace(old, "    names = [name for name, _value in browser_headers(profile)]\n    return list(reversed(names))", 1))
PY
check "frozen-headers: the header order was reversed" \
  run_gate /tmp/mut-hdr-order audit-frozen-headers.py

# 3. AsyncSession gains a different default for a shared option.
scratch /tmp/mut-parity-default
python3 - <<PY
import pathlib
p = pathlib.Path("/tmp/mut-parity-default/bindings/python/chrome_client/_python_impl/sessions.py")
lines = p.read_text().split("\n")
hits = [i for i, line in enumerate(lines)
        if 'proxy_chain=None, platform="auto", max_sockets_per_group=0):' in line]
assert len(hits) == 2, hits
lines[hits[-1]] = lines[hits[-1]].replace('platform="auto"', 'platform="windows"')
p.write_text("\n".join(lines))
PY
check "session-parity: a shared option defaults differently" \
  run_gate /tmp/mut-parity-default audit-session-parity.py

# 4. AsyncSession stops accepting an option Session accepts.
scratch /tmp/mut-parity-missing
python3 - <<PY
import pathlib
p = pathlib.Path("/tmp/mut-parity-missing/bindings/python/chrome_client/_python_impl/sessions.py")
lines = p.read_text().split("\n")
hits = [i for i, line in enumerate(lines)
        if 'proxy_chain=None, platform="auto", max_sockets_per_group=0):' in line]
lines[hits[-1]] = "                 proxy_chain=None, max_sockets_per_group=0):"
p.write_text("\n".join(lines))
PY
check "session-parity: an option went missing" \
  run_gate /tmp/mut-parity-missing audit-session-parity.py

# 5. discard_cookies goes back to a shared engine: resumption and cache return.
scratch /tmp/mut-discard-shared
python3 - <<PY
import pathlib
p = pathlib.Path("/tmp/mut-discard-shared/bindings/python/chrome_client/_python_impl/sessions.py")
s = p.read_text()
old = "        return EngineSlot(config.build(), config, single_use=True)"
assert old in s, "throwaway construction site moved; update this mutation"
p.write_text(s.replace(old, "        return self._engines.get(config)", 1))
PY
check "discard-fingerprint: the engine is shared again" \
  run_gate /tmp/mut-discard-shared audit-discard-fingerprint.py

# 6. A layer-crossing table is declared twice in one file. The gate compares
# (path, pattern_index), so before this case existed a second declaration of a
# table already listed in KNOWN collapsed onto the first and the gate passed.
scratch /tmp/mut-own-duplicate
python3 - <<PY
import pathlib
p = pathlib.Path("/tmp/mut-own-duplicate/bindings/python/chrome_client/_python_impl/sessions.py")
s = p.read_text()
needle = "def merge_setting(request_setting, session_setting, dict_class=CaseInsensitiveDict):"
assert needle in s, "merge_setting moved; update this mutation"
block = '_CORE_OWNED_HEADERS = frozenset((\n    "x-injected",\n))\n\n\n'
p.write_text(s.replace(needle, block + needle, 1))
PY
check "layer-ownership: a table is declared twice in one file" \
  run_gate /tmp/mut-own-duplicate audit-layer-ownership.py

# 7. A Core artifact records a source fingerprint that no longer matches the
# tree. Nothing compared core/source/ with core/binaries/ before this gate: a
# +11 diff sat in core/source/engine.cc for a full round while every other gate
# stayed green, because the manifests recorded the artifact hash and the
# Chromium revision but nothing about the sources.
scratch /tmp/mut-fingerprint-stale
cp -r "$REPO/core" /tmp/mut-fingerprint-stale/
python3 /tmp/mut-fingerprint-stale/tools/audit-core-source-fingerprint.py \
  --record > /dev/null
printf '\n// mutation probe\n' >> \
  /tmp/mut-fingerprint-stale/core/source/engine.cc
check "core-fingerprint: the artifact predates the sources" \
  run_gate /tmp/mut-fingerprint-stale audit-core-source-fingerprint.py

rm -rf /tmp/mut-hdr-value /tmp/mut-hdr-order /tmp/mut-parity-default \
       /tmp/mut-parity-missing /tmp/mut-discard-shared /tmp/mut-own-duplicate \
       /tmp/mut-fingerprint-stale
exit $FAILED
