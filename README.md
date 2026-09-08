# DCPrd 产品需求文档管理系统

自托管的产品需求文档（HTML / Axure、墨刀导出 zip）托管平台：上传即得公网免登录分享链接，所有历史版本永久保留，团队成员通过 Telegram 登录。完整需求见 [SPEC.md](SPEC.md)。

- 线上地址：<https://dcpm.ddns.net>
- 技术栈：Python 3.12 · FastAPI · Jinja2 · SQLite（WAL）· uvicorn · Caddy（自动 HTTPS）· Docker Compose
- 仓库公开，**任何密钥都不得提交**（`.env` 已在 `.gitignore`，模板见 `.env.example`）

## 目录结构

```
app/                FastAPI 应用
  config.py         .env / 环境变量 → Config
  db.py             SQLite 连接、schema、settings、审计日志
  security.py       Telegram 签名校验、分享码、Cookie 签名
  storage.py        上传落盘、zip 安全解包、入口判定、重新发布
  web.py            请求上下文（登录态 / CSRF / flash / 模板渲染）
  queries.py        常用查询
  routes/           auth · requirements · documents · versions · admin · public
templates/  static/ 服务端模板与自写 CSS/JS（无前端构建链）
tests/              pytest 接口测试 + Playwright 端到端测试
deploy/             install.sh · 自动更新脚本 · systemd unit
Dockerfile · docker-compose.yml · Caddyfile
```

## 一条命令安装（服务器）

以 root 在目标服务器执行（Amazon Linux 2023 / Ubuntu / Debian）：

```bash
curl -fsSL https://raw.githubusercontent.com/ChromeTokyo/DCPrd/main/deploy/install.sh | sudo TG_BOT_TOKEN=<bot token> bash
```

脚本是幂等的，做了这些事：安装 docker + compose 插件 → clone/更新 `/opt/dcpm` → 生成 `/opt/dcpm/.env`（已存在则保留）→ `docker compose up -d --build` → 安装每分钟检查 GitHub `main` 的自动更新 timer。

前置条件：DNS 已指向服务器（`dig dcpm.ddns.net`），安全组放通 80/443；Telegram 里对 `@BotFather` 执行 `/setdomain` 把 `@dcprd_bot` 绑定到 `dcpm.ddns.net`，否则登录页不会出现 Telegram 按钮。

## 常用运维

```bash
cd /opt/dcpm
docker compose ps                       # 状态
docker compose logs -f app              # 应用日志
docker compose logs -f caddy            # 证书签发 / 代理日志
sudo /usr/local/bin/dcpm-update.sh      # 手动触发更新（同 timer 逻辑）
tail -f /var/log/dcpm-update.log        # 自动更新日志
systemctl status dcpm-update.timer      # 自动更新 timer
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build   # 手动重建
curl -sS https://dcpm.ddns.net/healthz  # {"ok":true,"version":"<git sha>"}
```

发布流程：push 到 GitHub `main` → 1～2 分钟内服务器自动 `git reset --hard origin/main && docker compose up -d --build`，`/healthz` 的版本号随之变化。

### 数据与备份

所有数据在 `/opt/dcpm/data`（容器内 `/data`）：

```
data/db/dcpm.sqlite            数据库（WAL 模式）
data/docs/<doc_id>/v<n>/       content/（解压后内容）与 original/（原始上传文件）
data/backups/dcpm-YYYYMMDD.sqlite   每日快照（应用启动时 + 每 24h，保留 14 天）
data/secret_key                会话签名密钥（.env 未提供 SECRET_KEY 时自动生成）
```

**恢复**：停止应用 → 用快照覆盖数据库 → 启动。

```bash
cd /opt/dcpm && docker compose stop app
cp data/backups/dcpm-20260908.sqlite data/db/dcpm.sqlite
rm -f data/db/dcpm.sqlite-wal data/db/dcpm.sqlite-shm
docker compose start app
```

整机迁移只需拷贝 `/opt/dcpm/data` 与 `/opt/dcpm/.env`。删除均为软删除，误删的需求/文档/版本可由技术人员直接在 SQLite 中把 `deleted_at` 置空恢复。

## 本地开发

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env    # 本地把 DEV_MODE=1、DATA_DIR=./data、DOMAIN=localhost:8000、TG_BOT_TOKEN=123456:TEST
.venv/bin/uvicorn app.main:get_app --factory --reload
```

本地无法真的点 Telegram 按钮，可用与测试相同的方式自算签名直接访问 `/auth/telegram`（见 `tests/conftest.py` 的 `tg_params`）。

## 测试

```bash
.venv/bin/python -m pytest -q --ignore=tests/test_e2e.py     # 接口/单元测试
.venv/bin/python -m pytest -q tests/test_e2e.py --browser chromium   # Playwright 端到端
```

## 实现要点与取舍（SPEC 未细述处的决定）

- **上传限流**：中间件先按 `Content-Length` 拒绝超限请求（413），随后把文件按 1MB 分块边读边写临时文件并计数，超限即中止；Caddy 另有 400MB 请求体上限。
- **每请求一个 SQLite 连接**，`isolation_level=None`（自动提交）+ `busy_timeout=10s`，WAL 模式；schema 用 `CREATE TABLE IF NOT EXISTS` + `schema_version`。
- **单点登录**：同一账号只保留一个有效会话，在另一处登录后原设备立即退出。
- **会话永不过期**（需求方 2026-09-08 决定，覆盖 SPEC 的 30 天）：`sessions` 表 + 签名 Cookie（`HttpOnly; SameSite=Lax; Secure`，`DEV_MODE=1` 时不加 Secure），Cookie 按浏览器上限 400 天下发并在访问时滑动续期；只有手动退出、被解绑/删除用户才失效。
- **分享链接固定不变**：上传新版本不改变 `/s/<code>/`，旧链接始终指向最新版；历史版本用 `/v/<code>/<n>/`（后台版本表可复制）。只有明确点「重置分享链接」才会换码。
- **删除用户**为软删除（保留其在版本记录、审计日志中的姓名），并解除 Telegram 绑定、移出负责人列表。
- **复合需求转换**不做反向；单体需求删除只删需求本身（其唯一文档随需求一起隐藏）。
- **zip**：拒绝绝对路径 / `..`，跳过 `__MACOSX`、`.DS_Store`、`Thumbs.db`、符号链接；文件名按 utf-8 → gbk 顺序还原；单一顶层目录自动剥离；解压总量 ≤ 4×上传上限、文件数 ≤ 100000。
- **公开文档**：`/s/` 为 `no-cache`，`/v/` 为一年 immutable；html 响应按 `<meta charset>` 输出 charset；沙箱开关默认开（CSP `sandbox` 不含 `allow-same-origin`）。
- 时间统一存 UTC 字符串（`YYYY-MM-DDTHH:MM:SS`），展示时按系统设置的时区转换。

## 部署记录

- 2026-09-08：首次部署到 `57.180.39.231`（Amazon Linux 2023，`ec2-user`），Let's Encrypt 证书签发成功，自动更新 timer 已启用。
