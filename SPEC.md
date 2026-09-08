# DCPrd 产品需求管理系统 — 开发交付文档

> 本文档面向执行开发与部署的工程师 / Claude Code。需求已与需求方（Chrome）逐条确认，**文中所有"已定"事项不必再向需求方确认**，直接照做。遇到本文未覆盖的细节，按"内部团队工具、简单可靠优先"的原则自行决定，并在 README 中记录。

## 0. 怎么使用这份文档（给 Claude Code）

1. 在 Mac 上新建一个空目录（例如 `~/Projects/DCPrd`），把本文件放进去，命名为 `SPEC.md`。
2. 在该目录运行 `claude`，输入：
   > 请完整阅读 SPEC.md，按其中的技术方案实现整个系统，在本地跑通端到端测试后，推送到 GitHub 仓库并按第 9 节部署到服务器，最后按第 10 节逐项验收。敏感信息（Bot Token 等）我会在对话里单独给你，不要写进代码仓库。
3. 需求方需要单独提供给 Claude Code 的敏感信息（不要写进仓库、不要写进本文档）：
   - Telegram Bot Token（Bot 为 `@dcprd_bot`）
   - 服务器 SSH 私钥路径（与 GitHub 使用的是同一把 ed25519 密钥，通常在 `~/.ssh/id_ed25519`）
4. 需求方需要亲自做的两件事（代码做不到）：
   - 在 Telegram 里给 `@BotFather` 发 `/setdomain`，选择 `@dcprd_bot`，填 `dcpm.ddns.net`（否则网页上的 Telegram 登录按钮不会出现）。
   - 确认 AWS 安全组已放通 80、443 端口（22 已通）。

## 1. 项目背景与目标

产品团队需要一个自托管平台来管理产品需求文档：产品经理上传 HTML 格式的需求文档（单个 html 或 Axure/墨刀等工具导出的 zip 包），系统负责托管并生成**公网可直接访问、无需登录**的分享链接，同时完整保留每一次上传的历史版本（谁、何时、传了什么，永久保留）。团队成员通过 Telegram 授权登录，不设密码。

固定信息：

| 项目 | 值 |
|---|---|
| 线上域名 | `https://dcpm.ddns.net`（DNS 已指向服务器，部署前用 `dig` 复核） |
| 服务器 | `57.180.39.231`（AWS 东京），SSH 端口 22，用 ed25519 密钥登录；用户名未知，依次尝试 `ubuntu` / `ec2-user` / `admin` / `root` |
| 代码仓库 | `git@github.com:ChromeTokyo/DCPrd.git`（**公开仓库**，因此仓库里绝对不能出现任何密钥；SSH 密钥已加到 GitHub 账号） |
| Telegram Bot | `@dcprd_bot`，Token 由需求方在对话中提供 |
| Jira | 地址 `https://dcjira.opscom666.com/jira`，单号形如 `DC-85989`，链接格式 `https://dcjira.opscom666.com/jira/browse/DC-85989` |
| 三个大项目 | `eb`、`im`、`tk`（固定，不需要项目管理功能） |
| 界面语言 | 简体中文 |
| 时区 | 显示时区默认 `Asia/Tokyo`，可在系统设置中修改；数据库存 UTC |

## 2. 角色与权限（已定）

三种角色：`super`（超级管理员，唯一）、`admin`（管理员）、`user`（用户）。

**超级管理员**：系统初始化后**第一个完成 Telegram 登录的人**自动成为超级管理员（无需邀请）。拥有管理员全部权限，另外可将任意用户设为管理员或取消管理员。超级管理员不能被删除、解绑（自己除外）或降级。

**管理员**：新增用户、生成/重置邀请链接、解绑用户的 Telegram、删除用户；删除任何人创建的需求、子文档、历史版本（删除均需二次确认并写审计日志）；修改系统设置；查看审计日志。

**用户**：新建需求；编辑**任何**需求的信息（名称/Jira/负责人/备注）；给**任何**需求或子文档上传新版本（这是团队协作的基础——所有人可写，所有改动记录操作人与时间）。用户只能删除**自己创建的**需求/子文档；**任何普通用户都不能删除历史版本**。

**未登录访客**：只能通过分享链接查看文档，访问任何后台页面一律跳转登录页。

## 3. 登录与账号（已定）

### 3.1 邀请绑定

1. 管理员在「用户管理」输入成员名称（如"张三"）→ 系统创建用户并生成一次性邀请链接 `https://dcpm.ddns.net/invite/<随机码>`（随机码 ≥ 24 字节 url-safe）。
2. 管理员复制链接发给对方。对方打开后看到"你好 张三，请使用 Telegram 登录以完成绑定"和 Telegram 登录按钮。
3. 授权成功 → 该 Telegram 账号与"张三"绑定，邀请链接立即失效；记录 Telegram ID、用户名、姓名、头像。
4. 若该 Telegram 已绑定其他用户 → 报错，不允许重复绑定。
5. 管理员可「解绑」（清除绑定并可重新生成邀请链接）或「删除用户」；两者都立即使该用户所有会话失效。

### 3.2 日常登录

登录页只有一个 Telegram 登录按钮。已绑定的账号一点即登录；未绑定的账号提示"该 Telegram 尚未被邀请，请联系管理员"。会话 30 天，可手动退出。

### 3.3 初始化

数据库中没有任何已绑定用户时，登录页显示"系统初始化：第一个登录的 Telegram 账号将成为超级管理员"，登录即成为 super。之后该逻辑永久关闭。

### 3.4 Telegram Login Widget 技术要点

- 页面嵌入：`<script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="dcprd_bot" data-size="large" data-auth-url="https://dcpm.ddns.net/auth/telegram" data-request-access="write"></script>`
- 回调 `GET /auth/telegram` 收到 query：`id, first_name, last_name?, username?, photo_url?, auth_date, hash`。
- 校验：把除 `hash` 外的所有字段按 key 字典序排成 `key=value` 并用 `\n` 连接得到 `data_check_string`；`secret_key = SHA256(bot_token)`；`HMAC_SHA256(secret_key, data_check_string)` 的 hex 必须等于 `hash`；且 `now - auth_date < 86400`。
- 邀请码传递：进入 `/invite/<码>` 时把邀请码写入一个 10 分钟有效的 HttpOnly Cookie，回调时读取，不依赖 Telegram 把 query 参数原样带回。
- 本地开发/自动化测试无法真的点 Telegram：测试代码用配置里的 bot token **自己算出合法的 hash** 直接请求 `/auth/telegram`，这样能覆盖真实校验路径；不要做绕过校验的后门。

## 4. 业务模型（已定）

### 4.1 需求（requirement）

分**单体需求**（一个需求 = 一份文档）与**复合需求**（一个需求下挂多份子文档，每份子文档有自己的名称、备注、版本历史与分享链接，复合需求本身还有一个公开"目录页"链接）。

| 字段 | 说明 |
|---|---|
| project | `eb`/`im`/`tk`，必填 |
| kind | `single` / `compound` |
| name | 必填 |
| jira_keys | 文本，可填多个（空格/逗号分隔），显示时每个单号渲染成链接 `<JIRA_BASE_URL>/browse/<单号>` |
| owners | 负责人，多选自系统用户（未删除的），可多人 |
| notes | 多行备注，展示时转义后保留换行、URL 自动变链接 |
| share_code | 复合需求目录页的公开码 |
| created_by/at, updated_by/at, deleted_at/by | 系统维护；**删除一律软删除**（`deleted_at`），文件不物理删除 |

单体需求可「转为复合需求」：kind 改为 compound，原文档成为第一个子文档，历史全部保留。不需要反向转换。

### 4.2 文档（document）

属于一个需求。字段：`requirement_id, name, notes, share_code(唯一), position, created_by/at, updated_at, deleted_at/by`。单体需求恰有一个文档（name 与需求同名，需求改名时同步）；复合需求 0..n 个。

### 4.3 版本（version）

每次上传生成一个版本，编号从 v1 递增（同一文档内唯一，删除也不复用编号）。字段：
`document_id, number, kind(html|zip), original_filename, size_bytes, file_count, entry_path(可空), html_candidates(JSON,可空), note(本次更新说明,可选), source_version_id(重新发布来源,可空), uploaded_by, uploaded_at, deleted_at/by`。

- **上传形态**：单个 `.html/.htm`，或 `.zip`。zip 处理规则：
  - 防 zip-slip：拒绝绝对路径和含 `..` 的条目；跳过 `__MACOSX/`、`.DS_Store`、`Thumbs.db`。
  - 文件名编码：Python zipfile 对非 UTF-8 标记的名字按 cp437 解码，需要还原字节后依次尝试 utf-8、gbk（Windows 中文压缩包常见）。
  - 若所有条目都在同一个顶层目录下，自动去掉这一层。
  - 限制：解压后总大小 ≤ 4 × 上传上限，文件数 ≤ 100000。
  - **入口文件判定**：根目录有 `index.html` → 用它；否则根目录恰有一个 `.html` → 用它；否则整包只有一个 `.html` → 用它；否则 `entry_path` 留空、`html_candidates` 存全部 html 路径（最多 500 个），上传后跳到"选择入口文件"页面让上传者单选。入口未选定的版本不算已发布（不会成为"最新版"），列表中标记"待选择入口"。
- 单个 html 上传：存为 `content/<原文件名>`，entry_path 即该文件名。
- **原始上传文件也保留**（`original/<原文件名>`），提供"下载原文件"（需登录）。
- **所有历史版本永久保留**；普通用户不能删版本；管理员删除为软删除 + 审计日志。
- 「以此版本重新发布」：把某历史版本复制为一个新的最新版本（文件复制或硬链接），`source_version_id` 指向来源，note 自动填"重新发布自 v3"。
- 上传大小上限默认 300 MB，系统设置可改；服务端边读边写临时文件并计数，超限即中止。

「最新版本」= 该文档中 `deleted_at IS NULL AND entry_path IS NOT NULL` 的最大 number。

### 4.4 存储布局

```
/data/db/dcpm.sqlite          SQLite 数据库（WAL 模式）
/data/docs/<document_id>/v<n>/content/...     解压后/单文件内容
/data/docs/<document_id>/v<n>/original/<原文件名>
/data/backups/dcpm-YYYYMMDD.sqlite            每日数据库快照，保留 14 天
/data/secret_key                              首次启动自动生成的会话签名密钥（若 .env 未提供）
```

## 5. 公开访问（分享链接，已定）

| 链接 | 行为 |
|---|---|
| `https://dcpm.ddns.net/s/<doc_code>/` | 文档**最新版本**入口页；无需登录；`Cache-Control: no-cache` |
| `https://dcpm.ddns.net/s/<doc_code>/<相对路径>` | 最新版本内的资源（zip 包里的图片/CSS/JS 按原目录结构） |
| `https://dcpm.ddns.net/v/<doc_code>/<n>/` 与 `/v/<doc_code>/<n>/<相对路径>` | 指定历史版本，内容不变，可长缓存 |
| `https://dcpm.ddns.net/s/<req_code>/` | 复合需求目录页：需求名 + 各子文档的最新版链接（不展示备注等内部信息） |

细节：

- `/s/<code>` 无尾斜杠 → 301 到带斜杠，保证相对路径解析正确。入口文件不在根目录时（如 `proto/index.html`），`/s/<code>/` 302 到 `/s/<code>/proto/index.html`。
- 分享码：12 位，字母表 `abcdefghjkmnpqrstuvwxyz23456789`，在 documents 与 requirements 两张表中全局唯一。
- 「重置分享链接」：生成新码，旧链接立即 404（用于泄露止损）。
- Content-Type：按扩展名；`.html/.htm` 先读前 4KB 找 `<meta charset>`，找到就用它（可能是 gbk/gb2312），找不到默认 utf-8。注意 Starlette 的 FileResponse 会自动补 `charset=utf-8`，需要手动覆盖 header。
- 路径必须规范化并确认落在该版本的 `content/` 目录内，否则 404。
- **脚本沙箱**：用户上传的 HTML 与系统同源，理论上恶意脚本可冒用登录者身份。系统设置提供开关「文档脚本沙箱」（默认**开**）：开启时公开文档的 html 响应带 `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation`（**不含** `allow-same-origin`）。若 Axure 等原型在沙箱下显示异常（多 iframe 互访、localStorage 报错），管理员可关闭该开关；关闭后依靠 HttpOnly Cookie + CSRF Token 兜底。在设置页面用一句话说明这个取舍。
- 公开页不带 `X-Frame-Options`（允许被其他系统嵌入）；后台页面带 `X-Frame-Options: DENY`。

## 6. 页面与路由清单

后台页面均需登录（未登录 → 302 `/login?next=...`，next 只接受站内相对路径）。所有 POST 校验 CSRF（表单隐藏字段，Token 绑定会话）。删除类操作用**页面内自定义确认弹层**，不要用 `window.confirm`（会卡住浏览器自动化测试）。

| 路由 | 说明 |
|---|---|
| `GET /login` | Telegram 登录按钮；初始化提示；未邀请提示 |
| `GET /invite/{code}` | 显示被邀请人姓名 + 登录按钮；无效/已用 → 友好提示 |
| `GET /auth/telegram` | Widget 回调；校验 → 绑定/登录 → 302 `/` |
| `POST /logout` | 退出 |
| `GET /` | 需求列表：项目 Tab（全部/EB/IM/TK）、关键词搜索（名称、Jira 单号、负责人姓名）、按最近更新倒序、每页 50。列显示：名称（含单体/复合标签）、Jira 链接、负责人、最新版本（vN · 上传人 · 时间）、复制分享链接按钮、更新时间 |
| `GET/POST /req/new` | 新建需求；单体需求可同时上传首个文件 |
| `GET /req/{id}` | 需求详情：信息区、操作区（编辑 / 转为复合 / 重置分享链接 / 删除）；单体：分享链接框 + 版本表 + 上传表单；复合：目录链接 + 子文档列表（名称、最新版、复制链接、进入）+ 新增子文档表单（名称 + 备注 + 可选文件） |
| `GET/POST /req/{id}/edit`、`POST /req/{id}/delete`、`POST /req/{id}/convert`、`POST /req/{id}/reset-share`、`POST /req/{id}/docs/new` | 见名称 |
| `GET /doc/{id}` | 子文档详情：名称/备注编辑、分享链接、版本表、上传表单、删除 |
| `POST /doc/{id}/edit`、`POST /doc/{id}/delete`、`POST /doc/{id}/reset-share`、`POST /doc/{id}/upload` | 上传字段：`file`、`note` |
| `GET/POST /ver/{id}/entry` | 选择 zip 入口文件 |
| `POST /ver/{id}/republish`、`POST /ver/{id}/delete`（admin）、`GET /ver/{id}/download` | 版本操作 |
| `GET /admin/users`、`POST /admin/users/new`、`POST /admin/users/{id}/regen-invite`、`/unbind`、`/delete`、`/toggle-admin`（super） | 用户管理 |
| `GET/POST /admin/settings` | 站点名称、Jira 地址、上传上限、时区、文档脚本沙箱；只读展示域名、Bot 用户名、当前版本号 |
| `GET /admin/audit` | 审计日志（最近 500 条）：删除、解绑、权限变更、重置分享链接、重置邀请、设置修改 |
| `GET /s/...`、`GET /v/...` | 公开访问，见第 5 节 |
| `GET /healthz` | `{"ok":true,"version":"<git sha>"}` |

版本表列：版本号、上传人、上传时间、文件（名称/大小/类型）、更新说明、操作（查看 / 复制链接 / 重新发布 / 下载原文件 / 删除-仅管理员）。

UI 要求：简洁的浅色后台风格，自写 CSS（不引入前端构建链），桌面优先、手机上可阅读；项目用不同颜色的小标签区分；复制链接用 `navigator.clipboard` 并给出"已复制"提示；操作成功/失败用一次性 flash 提示。

## 7. 技术方案（已定）

- **语言/框架**：Python 3.12 + FastAPI + Jinja2 服务端渲染，`python-multipart`（上传）、`itsdangerous`（Cookie 签名）、uvicorn。数据库用标准库 `sqlite3`，WAL 模式，`busy_timeout`；每个请求独立连接。schema 用 `CREATE TABLE IF NOT EXISTS` + `schema_version` 表，方便以后迁移。
- **会话**：`sessions` 表（id 随机 token、user_id、csrf、created/expires/last_seen）；Cookie 存签名后的 session id，`HttpOnly; Secure; SameSite=Lax`（Lax 而非 Strict——Telegram 回调是跨站跳转）。解绑/删除用户时删除其全部会话。
- **配置（.env）**：`DOMAIN`、`TG_BOT_TOKEN`、`TG_BOT_USERNAME`、`JIRA_BASE_URL`、`SECRET_KEY`（可空，自动生成到 `/data/secret_key`）、`MAX_UPLOAD_MB`（默认 300）、`TIMEZONE`（默认 Asia/Tokyo）、`DATA_DIR`（默认 /data）、`DEV_MODE`（本地开发时允许非 Secure Cookie）。可在后台修改的项以数据库 `settings` 表为准，.env 只作首次默认值。
- **容器**：`Dockerfile` 基于 `python:3.12-slim`；`docker-compose.yml` 两个服务：`app`（uvicorn :8000，`--proxy-headers --forwarded-allow-ips='*'`，挂载 `./data:/data`，`env_file: .env`，构建参数注入 git sha 作为版本号）和 `caddy`（`caddy:2`，映射 80/443，挂载 Caddyfile 与 caddy_data/caddy_config 卷）。两者 `restart: unless-stopped`。
- **Caddyfile**：
  ```
  {$DOMAIN} {
      encode gzip
      request_body {
          max_size 400MB
      }
      reverse_proxy app:8000
  }
  ```
  Caddy 自动申请并续期 Let's Encrypt 证书，前提是 80/443 可达且 DNS 正确。
- **备份**：应用启动时以及此后每 24 小时用 `sqlite3` 的 backup API 把数据库快照到 `/data/backups/dcpm-YYYYMMDD.sqlite`，删除 14 天前的快照。
- **审计日志**：`audit_log(at, user_id, user_name, action, target_type, target_id, detail)`。
- **安全清单**：Telegram hash 校验 + auth_date 时效；CSRF；HttpOnly Cookie；上传大小限制；zip-slip 防护；公开文件路径越界防护；CSP sandbox 开关；后台页 `X-Frame-Options: DENY`、`Referrer-Policy: same-origin`；仓库里不出现任何密钥（`.env` 在 `.gitignore`，提供 `.env.example`）。

## 8. 测试要求

- **单元/接口测试**（pytest + httpx）：Telegram 签名校验（合法、篡改、过期）、初始化超级管理员、邀请绑定与重复绑定、权限矩阵（用户删他人需求 403、管理员可删、普通用户删版本 403、super 才能 toggle-admin）、zip 安全（`..` 条目、顶层目录剥离、gbk 文件名、入口判定的四种分支）、分享链接（最新版跟随、版本链接固定、重置后旧码 404、路径越界 404）、软删除后不可见。
- **端到端测试**（Playwright，Chromium）：模拟登录（自算 hash）→ 新建用户与邀请 → 第二个身份通过邀请绑定 → 新建单体需求并上传 html → 打开公开链接确认渲染 → 上传 v2 确认 `/s/` 跟随最新、`/v/../1/` 仍是旧内容 → 新建复合需求、添加两个子文档、上传 zip（含 `images/` 子目录，构造一个带图片引用的测试包）→ 公开页图片加载成功 → 目录页列出子文档 → 转为复合 → 管理员删除他人文档、审计日志有记录。
- 本地跑测试时用假的 bot token（如 `123456:TEST`），生产 token 只在服务器 .env。

## 9. 部署步骤（Claude Code 通过 SSH 执行）

1. `ssh -i ~/.ssh/id_ed25519 <user>@57.180.39.231`，确认系统（`cat /etc/os-release`）。Ubuntu/Debian 用 `curl -fsSL https://get.docker.com | sh`；Amazon Linux 2023 用 `dnf install -y docker git` 并手动安装 compose 插件到 `/usr/local/lib/docker/cli-plugins/`。`systemctl enable --now docker`。
2. `git clone https://github.com/ChromeTokyo/DCPrd.git /opt/dcpm`（公开仓库，匿名 clone）。
3. 写 `/opt/dcpm/.env`（`chmod 600`）：`DOMAIN=dcpm.ddns.net`、`TG_BOT_TOKEN=<需求方提供>`、`TG_BOT_USERNAME=dcprd_bot`、`JIRA_BASE_URL=https://dcjira.opscom666.com/jira`、`SECRET_KEY=$(openssl rand -hex 32)`、`MAX_UPLOAD_MB=300`、`TIMEZONE=Asia/Tokyo`。
4. `cd /opt/dcpm && docker compose up -d --build`；`docker compose logs -f caddy` 观察证书签发成功。
5. **自动更新**：安装 `deploy/dcpm-update.sh`（`git fetch origin main`，若本地与远端不同则 `git reset --hard origin/main && docker compose up -d --build`，输出到 `/var/log/dcpm-update.log`）+ systemd `dcpm-update.service` / `dcpm-update.timer`（每 1 分钟，`OnBootSec=2min`），`systemctl enable --now dcpm-update.timer`。这样以后只需 push 到 `main` 分支即可上线。
6. 把上述步骤写成 `deploy/install.sh`（幂等、可重复执行）放进仓库，并在 README 写清一条命令的安装方式与常用运维命令（看日志、手动更新、备份位置、恢复步骤）。
7. 验证：`curl -sS https://dcpm.ddns.net/healthz` 返回版本号；浏览器打开登录页看到 Telegram 按钮（需求方完成 `/setdomain` 之后）。

## 10. 验收清单

- [ ] `https://dcpm.ddns.net` 有有效 HTTPS 证书，http 自动跳 https。
- [ ] 需求方用自己的 Telegram 首次登录成为超级管理员；第二个人在没有邀请的情况下登录被拒。
- [ ] 新增用户 → 复制邀请链接 → 对方 Telegram 登录后绑定成功；同一 Telegram 不能再绑第二个用户；解绑后重新生成链接可绑定新账号。
- [ ] 超级管理员能设置/取消管理员；管理员看不到该按钮。
- [ ] 三个项目 Tab 与搜索可用。
- [ ] 单体需求：上传 html → 分享链接公网免登录可打开（用手机流量验证）；上传 v2 后固定链接显示新内容，v1 链接仍显示旧内容；版本表记录上传人、时间、说明。
- [ ] zip 包（Axure 导出）上传后图片/CSS/JS 正常加载；无 index.html 的包会要求选择入口。
- [ ] 复合需求：多个子文档各自有链接与版本历史；目录页列出全部子文档；单体可转复合。
- [ ] Jira 单号自动链接到 `https://dcjira.opscom666.com/jira/browse/<单号>`。
- [ ] 普通用户不能删除他人需求与任何版本；管理员可以；审计日志有记录。
- [ ] 重置分享链接后旧链接 404。
- [ ] 推送一个小改动到 GitHub `main`，1～2 分钟内线上 `/healthz` 版本号变化。
- [ ] `/data/backups/` 出现每日快照。

## 11. 未纳入本期（不要做）

需求状态字段、Telegram 消息通知、全文检索、版本 diff、分享链接密码/关闭公开、评论批注、回收站 UI（软删除数据保留在库中即可）。

## 变更记录

- 2026-09-08 v1：需求确认稿。
- 2026-09-08 v2：改写为开发交付文档，补充技术方案、路由、存储、部署与验收细节；确定 Jira 地址、GitHub 仓库；开发与部署改由需求方本机的 Claude Code 执行（云端环境无法 SSH/推送）。
