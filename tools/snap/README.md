# dcpm-snap：页面快照工具

把一个后台（Vue/React 等 HTML 应用）或 Flutter Web 端，按菜单逐页抓成**自包含静态文件**，打包为 DCPrd 2.0 可直接导入的 zip。每次生产发布后跑一次，就得到该 release 的"线上基线"。

- HTML 应用：保存整页 DOM，CSS 内联、图片/字体转 data URI、脚本移除、表单值冻结、canvas 转图片 → 一个 `.html`，可直接给 AI 修改。
- Flutter Web（canvas 渲染，DOM 无内容）：整页长截图 `.png`，系统用像素对比圈出改动。

## 安装（任何能访问 UAT 的机器）

```bash
python3 -m venv .snap && . .snap/bin/activate      # Windows: .snap\Scripts\activate
pip install playwright
python -m playwright install chromium
```

## 三步用法

1. **准备菜单/路由 JSON**：请前端导出路由配置（`routes.json`），常见形状都能识别：
   `[{ "path": "/merchant", "meta": {"title": "商户管理"}, "children": [{ "path": "list", "meta": {"title": "商户列表"} }] }]`，
   也可用 `title/name`、`route/path`、`children/routes`，或包在 `{"routes": [...]}` 里。子路由相对路径会自动拼接；`hidden: true` 的会跳过。
   没有路由文件时，可以直接在配置里写 `menu`（见 `example-config-flutter.json`）。
2. **登录一次**：`python snap.py --config eb-ops.json --login`，在弹出的浏览器里登录后回终端按回车，登录态保存到 `state.json`（有效期取决于业务系统）。
   也可以用表单自动登录：`"login": {"type": "form", "url": "/login", "user_selector": "input[name=username]", "pass_selector": "input[type=password]", "submit_selector": "button[type=submit]", "user_env": "SNAP_USER", "pass_env": "SNAP_PASS"}`，账号密码放环境变量。
3. **抓取**：`python snap.py --config eb-ops.json --release 3.12.0`，得到 `snapshot-eb-ops-admin-3.12.0.zip`，在 DCPrd 2.0「系统与端 → 该端 → 导入 release 快照」上传。

调试：`--limit 3` 只抓前 3 页；`--only /merchant` 只抓某前缀；`--headful` 看着浏览器抓。抓取结果在 zip 内 `report.json`，失败的页面会列出原因。

## 配置项

| 字段 | 说明 |
|---|---|
| `app` | 端标识，必须与 DCPrd 里该端的「标识」一致 |
| `release` | 线上版本号（可用 `--release` 覆盖） |
| `base_url` | UAT / 预发地址 |
| `kind` | `web` / `flutter_web` / `h5`；`flutter_web` 自动用截图 |
| `renderer` | `html` 或 `image` |
| `hash_routing` / `route_template` | 前端用 `#/path` 路由时设 `hash_routing: true`；特殊拼接用 `route_template`，如 `"{base_url}/app/#{route}"` |
| `routes` / `menu` | 路由 JSON 文件路径，或直接内联菜单 |
| `viewport`、`device_scale_factor` | 视口；App 用手机尺寸 + 2 倍缩放 |
| `wait` | `network_idle`（等网络空闲）、`selector`（等某元素出现）、`ms`（额外等待） |
| `mask` | 动态区域选择器：抓 HTML 时把叶子文字替换为 `—` 并灰底；避免列表数据、时间戳造成 diff 噪音 |
| `remove` | 抓取前移除的元素（弹窗、客服挂件） |
| `only` / `exclude` | 路由前缀过滤 |
| `login_check_selector` | 若页面上出现该元素（如密码框）则判定登录失效 |

## 建议

- UAT 用一套**固定的测试数据**，列表页尽量 `mask` 掉表体，只保留表头和操作区，这样版本之间的 diff 才聚焦于结构与文案变化。
- 上千页面一次抓完约 20～60 分钟（每页 1～3 秒），建议按端分开跑；导入时内容没变的页面不会产生新版本。
- 每次生产发布后由发布负责人跑一次并导入，或者放到定时任务里：`0 3 * * * cd /path && python snap.py --config eb-ops.json --release $(cat VERSION)`。
