#!/usr/bin/env bash
# DCPrd 一键安装/重装脚本（幂等，可重复执行）。以 root 运行：
#   curl -fsSL https://raw.githubusercontent.com/ChromeTokyo/DCPrd/main/deploy/install.sh | sudo bash
# 首次安装需要在 /opt/dcpm/.env 里填写 TG_BOT_TOKEN（脚本会生成模板并提示）。
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ChromeTokyo/DCPrd.git}"
REPO_DIR="${REPO_DIR:-/opt/dcpm}"
DOMAIN="${DOMAIN:-dcpm.ddns.net}"
COMPOSE_VERSION="${COMPOSE_VERSION:-v2.35.1}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请以 root 运行（sudo bash install.sh）" >&2
    exit 1
fi

. /etc/os-release
echo "==> 系统：$PRETTY_NAME"

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        echo "==> docker 已安装：$(docker --version)"
    else
        case "$ID" in
            amzn)
                dnf install -y docker git
                ;;
            ubuntu|debian)
                apt-get update -y && apt-get install -y git curl
                curl -fsSL https://get.docker.com | sh
                ;;
            *)
                echo "未知发行版 $ID，请手动安装 docker 与 git" >&2
                exit 1
                ;;
        esac
    fi
    command -v git >/dev/null 2>&1 || { [ "$ID" = amzn ] && dnf install -y git || apt-get install -y git; }
    systemctl enable --now docker
    if ! docker compose version >/dev/null 2>&1; then
        echo "==> 安装 docker compose 插件 $COMPOSE_VERSION"
        ARCH=$(uname -m)
        mkdir -p /usr/local/lib/docker/cli-plugins
        curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
            -o /usr/local/lib/docker/cli-plugins/docker-compose
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    fi
    echo "==> $(docker compose version)"
}

clone_repo() {
    if [ -d "$REPO_DIR/.git" ]; then
        echo "==> 更新已有仓库 $REPO_DIR"
        git -C "$REPO_DIR" fetch -q origin main
        git -C "$REPO_DIR" reset -q --hard origin/main
    else
        echo "==> 克隆 $REPO_URL 到 $REPO_DIR"
        git clone -q "$REPO_URL" "$REPO_DIR"
    fi
}

write_env() {
    local env="$REPO_DIR/.env"
    if [ -f "$env" ]; then
        echo "==> 保留已有 $env"
    else
        echo "==> 生成 $env（请填写 TG_BOT_TOKEN）"
        cat >"$env" <<ENV
DOMAIN=${DOMAIN}
TG_BOT_TOKEN=${TG_BOT_TOKEN:-REPLACE_ME}
TG_BOT_USERNAME=${TG_BOT_USERNAME:-dcprd_bot}
JIRA_BASE_URL=${JIRA_BASE_URL:-https://dcjira.opscom666.com/jira}
SECRET_KEY=$(openssl rand -hex 32)
MAX_UPLOAD_MB=${MAX_UPLOAD_MB:-300}
TIMEZONE=${TIMEZONE:-Asia/Tokyo}
ENV
    fi
    chmod 600 "$env"
    mkdir -p "$REPO_DIR/data"
}

start_stack() {
    echo "==> 构建并启动容器"
    cd "$REPO_DIR"
    GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build --remove-orphans
}

install_timer() {
    echo "==> 安装自动更新 timer"
    install -m 755 "$REPO_DIR/deploy/dcpm-update.sh" /usr/local/bin/dcpm-update.sh
    install -m 644 "$REPO_DIR/deploy/dcpm-update.service" /etc/systemd/system/dcpm-update.service
    install -m 644 "$REPO_DIR/deploy/dcpm-update.timer" /etc/systemd/system/dcpm-update.timer
    touch /var/log/dcpm-update.log
    systemctl daemon-reload
    systemctl enable --now dcpm-update.timer
}

install_docker
clone_repo
write_env
start_stack
install_timer

echo
echo "==> 完成。健康检查：curl -sS https://${DOMAIN}/healthz"
echo "    日志：cd ${REPO_DIR} && docker compose logs -f"
if grep -q REPLACE_ME "$REPO_DIR/.env"; then
    echo "!!! 请编辑 ${REPO_DIR}/.env 填写 TG_BOT_TOKEN 后执行：cd ${REPO_DIR} && docker compose up -d"
fi
