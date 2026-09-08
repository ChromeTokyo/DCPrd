#!/usr/bin/env bash
# 自动更新：若 origin/main 有新提交则拉取并重建容器。由 systemd timer 每分钟调用。
set -euo pipefail
REPO_DIR="${REPO_DIR:-/opt/dcpm}"
LOG="${LOG:-/var/log/dcpm-update.log}"
cd "$REPO_DIR"
exec >>"$LOG" 2>&1

git fetch -q origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi
echo "[$(date -Is)] 更新 ${LOCAL:0:7} -> ${REMOTE:0:7}"
git reset -q --hard origin/main
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null || true
echo "[$(date -Is)] 完成，当前版本 $(git rev-parse --short HEAD)"
