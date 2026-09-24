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
