#!/usr/bin/env bash
# 把当前分支与全部 tag 同时推到 origin（komAAmok）与 backup（dafatyu），
# 并在推送后逐项校验两个远端与本地完全一致。任何一项不一致就非零退出。
#
#   tools/sync-remotes.sh            # 推送 + 校验
#   tools/sync-remotes.sh --check    # 只校验，不推送
#
# backup 的凭据放在仓库外的 $HOME/.config/chrome_client/backup.token（600），
# 不写进 URL、.git/config 或任何被提交的文件。
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

CHECK_ONLY=0
[[ ${1:-} == --check ]] && CHECK_ONLY=1

TOKEN_FILE=${BACKUP_TOKEN_FILE:-$HOME/.config/chrome_client/backup.token}

die() { printf "sync-remotes: %s\n" "$*" >&2; exit 1; }

# 凭据：先清空 helper 链，再挂自定义的。
# 不清空的话全局 credential.helper=store 会先命中 ~/.git-credentials 里的
# komAAmok，push 到 backup 会被拒（实测报 denied to komAAmok）。
backup_git() {
  local pw
  pw=$(cat "$TOKEN_FILE")
  git -c credential.helper= \
      -c credential.helper="!f() { echo username=dafatyu; echo password=$pw; }; f" \
      -c http.postBuffer=524288000 \
      "$@"
}

BRANCH=$(git symbolic-ref --short HEAD)
TAGS=$(git tag | sort -V)
TAG_COUNT=$(printf "%s\n" "$TAGS" | grep -c . || true)

if (( ! CHECK_ONLY )); then
  [[ -s $TOKEN_FILE ]] || die "missing backup token at $TOKEN_FILE (chmod 600)"
  [[ -z $(git status --porcelain) ]] || die "working tree is dirty; commit before syncing"

  printf "push origin (%s + %s tags)...\n" "$BRANCH" "$TAG_COUNT"
  git push origin "refs/heads/$BRANCH:refs/heads/$BRANCH"
  git push origin --tags

  printf "push backup (%s + %s tags)...\n" "$BRANCH" "$TAG_COUNT"
  backup_git push backup "refs/heads/$BRANCH:refs/heads/$BRANCH"
  backup_git push backup "refs/tags/*:refs/tags/*"
fi

fail=0
LOCAL_HEAD=$(git rev-parse HEAD)
ORIGIN_HEAD=$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1)
BACKUP_HEAD=$(backup_git ls-remote backup "refs/heads/$BRANCH" | cut -f1)

[[ $ORIGIN_HEAD == "$LOCAL_HEAD" ]] || { printf "MISMATCH origin %s != local %s\n" "$ORIGIN_HEAD" "$LOCAL_HEAD" >&2; fail=1; }
[[ $BACKUP_HEAD == "$LOCAL_HEAD" ]] || { printf "MISMATCH backup %s != local %s\n" "$BACKUP_HEAD" "$LOCAL_HEAD" >&2; fail=1; }

local_tags=$(printf "%s\n" "$TAGS" | grep . || true)
origin_tags=$(git ls-remote --tags origin | grep -v "\^{}" | awk "{print \$2}" | sed "s#refs/tags/##" | sort -V)
backup_tags=$(backup_git ls-remote --tags backup | grep -v "\^{}" | awk "{print \$2}" | sed "s#refs/tags/##" | sort -V)

if [[ $origin_tags != "$local_tags" ]]; then
  printf "MISMATCH origin tags:\n" >&2
  diff <(printf "%s\n" "$local_tags") <(printf "%s\n" "$origin_tags") >&2 || true
  fail=1
fi
if [[ $backup_tags != "$local_tags" ]]; then
  printf "MISMATCH backup tags:\n" >&2
  diff <(printf "%s\n" "$local_tags") <(printf "%s\n" "$backup_tags") >&2 || true
  fail=1
fi

(( fail )) && die "remotes are not in sync with local"
printf "sync-remotes: origin and backup both at %s, %s tags identical\n" "${LOCAL_HEAD:0:8}" "$TAG_COUNT"
