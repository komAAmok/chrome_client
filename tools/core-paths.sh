#!/usr/bin/env bash
# Shared path resolution for the Core scripts.
#
# Source this file; do not execute it. It defines:
#   chromium_src       - the pinned Chromium checkout
#   chromium_toolchain - the cross toolchain root (xwin / osxcross)
#
# The previous defaults were absolute developer paths (/home/sj/chromium/src),
# which broke on any other machine with a misleading "no Chromium checkout"
# error. The default is now discovered, and CHROMIUM_SRC still overrides it.

# shellcheck shell=bash

_chrome_client_repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

# Resolution order, first match wins:
#   1. $CHROMIUM_SRC (explicit override, also used by CI)
#   2. a sibling checkout: <parent-of-repo>/chromium/src
#   3. $HOME/chromium/src, the layout the existing evidence was captured on
# The fallback is only a default; a missing tree still fails with the env var
# named, so the error says what to set rather than where the script looked.
resolve_chromium_src() {
  if [[ -n ${CHROMIUM_SRC:-} ]]; then
    printf '%s\n' "$CHROMIUM_SRC"
    return
  fi
  local root sibling home
  root=$(_chrome_client_repo_root)
  sibling="$(dirname "$root")/chromium/src"
  if [[ -d $sibling ]]; then
    printf '%s\n' "$sibling"
    return
  fi
  home="${HOME:-}/chromium/src"
  if [[ -n ${HOME:-} && -d $home ]]; then
    printf '%s\n' "$home"
    return
  fi
  # Nothing found: return the sibling path so the caller's error message is
  # stable, and let it name CHROMIUM_SRC.
  printf '%s\n' "$sibling"
}

# Sets RG to the ripgrep binary or fails with an actionable message. Called by
# every Core script, because a bare `if rg -q ...` with no rg on PATH evaluates
# false: the check reports clean without having searched anything.
require_rg() {
  # `type -P` looks the executable up on PATH only. `command -v` also matches
  # shell functions, so it would resolve to the rg() wrapper below, and the
  # wrapper calling "$RG" would then call itself until the shell segfaults.
  if [[ -z ${RG:-} ]]; then
    RG=$(type -P rg || true)
  fi
  if [[ -z $RG || ! -x $RG ]]; then
    printf '%s: ripgrep (rg) is required and must be an executable on PATH; set RG to its path\n' "${0##*/}" >&2
    exit 1
  fi
}

# ripgrep wrapper for gate conditions. `if rg -q PATTERN PATH` treats *every*
# non-zero status as "no match", but ripgrep exits 2 for an unreadable path, a
# bad pattern or a usage error -- so a renamed directory, a typo in a glob or an
# uninstalled rg turned a real check into a silent pass. Only 0 (match) and
# 1 (no match) keep their meaning here; anything above is fatal.
rg() {
  local status=0
  "$RG" "$@" || status=$?
  case $status in
    0 | 1) return "$status" ;;
    *)
      printf '%s: ripgrep exited %s, so this check did not run: rg %s\n' \
        "${0##*/}" "$status" "$*" >&2
      exit 1
      ;;
  esac
}

resolve_chromium_toolchain() {
  if [[ -n ${CHROMIUM_TOOLCHAIN:-} ]]; then
    printf '%s\n' "$CHROMIUM_TOOLCHAIN"
    return
  fi
  local root sibling home
  root=$(_chrome_client_repo_root)
  sibling="$(dirname "$root")/chromium"
  if [[ -d $sibling ]]; then
    printf '%s\n' "$sibling"
    return
  fi
  home="${HOME:-}/chromium"
  printf '%s\n' "$home"
}
