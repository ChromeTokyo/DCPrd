# DCPrd 2.0 — 页面聚合式需求管理（SPEC v1.0，2026-09-08 已与需求方确认）

> 1.0（按需求单托管文档）保持可用，2.0 挂在 `/v2/`，入口先放在后台角落（系统设置页底部链接），成熟后再替换主入口。本文是讨论稿，**「待定」标记的地方需要需求方拍板**。

## 1. 目标

把需求管理从"每个需求单拿出一份零散文档"改为"以线上页面为主干，需求是页面上的一次次变更"：

- 能按 系统 → 端 → 菜单 找到任何一个线上页面，看到它现在的样子和完整的变更历史。
- 任何一个需求，都能看到它改了哪些页面、每页改了什么（自动圈出）、谁改的。
- 页面与需求双向可达：从页面跳到动过它的需求，从需求跳到它改的所有页面。

## 2. 领域模型

```
项目 project（eb / im / tk，沿用 1.0）
 └ 系统 system            例：EB 商户后台体系
    └ 端 app               例：运营后台（Web）、商户 App（Flutter，有 H5）
       ├ 发布 release      线上版本号，例 3.12.0；每个端独立
       └ 菜单节点 menu_node 树形；叶子节点绑定一个页面，非叶子只是分组
          └ 页面 page      最小单位，拥有一条版本时间线
             └ 页面版本 page_version
                 kind:  baseline（线上基线，来自某个 release 的快照）
                        proposal（需求版本，来自某个需求单的改动稿）
                 media: html（自包含静态 HTML）| image（长截图，Flutter Web 等 canvas 页面）
需求单 requirement（沿用 1.0 的表，Jira 单号）
 └ 需求页面改动 requirement_page  = 需求单 × 页面 × 一个 proposal 版本（可多个页面，可新增页面）
批注 annotation  挂在 page_version 上：矩形框 + 文字，自动 diff 生成的与人工画的都存这里
```

关键规则：

- **页面身份**由 `(app, route_key)` 决定，route_key 来自前端路由配置（如 `/merchant/list`）。菜单改名、移动分组不影响页面身份。
- **线上基线**：每个 release 导入一次快照。同一页面内容 hash 与上一基线相同则不新建版本（上千页面绝大多数每次发布都不变，靠这个去重）。
- **需求版本**：PM 基于某个基线版本（记录 `base_version_id`）上传改动稿。一个需求对同一页面多次上传 → 同一 requirement_page 下多个 proposal，取最新。
- **上线对齐**：需求在 Jira 上线后，下一次 release 快照进来时，系统按 `requirement.release_version` 把 proposal 与新基线关联（"该需求已在 3.13.0 上线"）；也允许管理员手动标记。
- 删除一律软删除，权限沿用 1.0（所有人可写、只能删自己的、管理员可删全部、版本只有管理员能删）。

## 3. 两个视图

### 3.1 菜单视图 `/v2/`
- 左侧：项目 → 系统 → 端 选择，下方菜单树（上千页面，需搜索框 + 懒展开 + 最近访问）。
- 右侧：页面查看器，默认展示**最新线上基线**，顶部显示当前 release 版本号与时间线（基线用实心点、需求版本用空心点）。
- 点时间线任一节点切换版本；选中一个需求版本时：
  - 叠加显示与其 `base_version` 的 diff 红框（可开关），列出改动清单（新增/删除/文字变更/属性变更）；
  - 右栏显示需求单信息（Jira、改动人、时间、说明）与**"同需求改动的其他页面"**，一键跳转。
- 页面级操作：基于此页发起改动（见第 5 节）、下载当前版本、复制分享链接。

### 3.2 需求单视图 `/v2/req`
- 列表：Jira 单号、名称、涉及页面数、状态（草稿 / 待上线 / 已上线于 x.y.z）、负责人、更新时间；支持按单号/名称/负责人/页面搜索。
- 详情：涉及页面列表（每行：菜单路径、基线版本号、需求版本上传人与时间、diff 数量），点击进入菜单视图对应节点；整单公开分享链接（一个目录页 + 各页面 diff 视图，免登录）。

## 4. 线上基线的获取：快照工具 `dcpm-snap`

放在仓库 `tools/snap/`，Python + Playwright，跑在能访问测试/预发环境的机器上（可以就是服务器）。

输入：一个端的配置文件
```yaml
app: eb-ops-admin
base_url: https://uat-ops.example.com
login: { type: form, url: /login, user_env: SNAP_USER, pass_env: SNAP_PASS }   # 或 cookie 注入
routes_from: ./routes.json          # 前端提供的路由/菜单配置（含 title、path、children）
renderer: html | screenshot         # Web 后台用 html；Flutter Web 用 screenshot
wait: { selector: "#app .loaded", timeout_ms: 8000 }
mask: [".timestamp", ".table-body"]  # 动态区域：快照时固定/清空，避免 diff 噪音
```
输出：`snapshot-<app>-<release>.zip`，内含 `manifest.json`（页面列表、route_key、标题、hash）与每页一个自包含 HTML（CSS 内联、图片转 data URI、去掉脚本）或 PNG 长图。通过 2.0 后台「导入快照」上传，或用 CLI 直接 POST 到 `/v2/api/snapshots`（管理员 token）。

分工：前端提供路由配置、测试账号与稳定的 UAT 环境；快照工具与导入由本系统负责。**不跑前端代码、不 mock 接口。**

## 5. 产出需求的流程

1. PM 在菜单视图找到页面，点「基于此页发起改动」→ 选择已有需求单或新建（填 Jira）。
2. 系统提供该页当前基线（默认最新，可选历史基线）HTML 下载；新增页面则选择"新建页面"，可基于同端任一页面作模板。
3. PM 在外部用 AI 修改 HTML，回到需求单把改好的文件上传到对应页面 → 生成 proposal 版本，自动计算 diff 并写入批注。
4. PM 可在查看器里补画矩形批注、改说明；可对同一页面重复上传覆盖。
5. 需求单所有页面就位后，复制整单分享链接给研发/测试。
6. 上线后由 release 快照自动对齐，或管理员手动标记"已上线于 x.y.z"。

并发：若页面已有其他进行中的需求版本，发起改动时提示，并允许选择基于该版本继续改（记录 `base_version_id` 指向它）。

## 6. 自动圈出（diff 引擎）

- **html**：解析两版 DOM，按 `id` / `data-*` / 结构路径 / 文本相似度匹配节点，输出变更集（新增、删除、文字、属性、移动）。用无头 Chromium 渲染两版，取变更节点的 bounding box，存为矩形批注（相对页面坐标 + 视口宽度）。查看器以 iframe 展示版本 HTML，叠加一层 SVG 画框。
- **image**：像素差分 → 膨胀 → 连通区域 → 合并为若干矩形。
- 噪音控制：快照阶段 `mask` 动态区域；页面可配置"忽略选择器"；结构大改时提示"改动超过 60%，建议人工圈选"。
- 限制说明：AI 改稿若整体重排，自动 diff 只能给出大块区域，需要人工补圈。

## 7. 存储与路由

```
/data/v2/pages/<page_id>/<version_id>/page.html | page.png   版本内容
/data/v2/snapshots/<app>/<release>.zip                       原始快照包
```
新表：`systems, apps, releases, menu_nodes, pages, page_versions, requirement_pages, annotations, snapshot_imports`。沿用 `users, sessions, requirements, audit_log`。

主要路由（均需登录，公开分享除外）：

| 路由 | 说明 |
|---|---|
| `GET /v2/` | 菜单视图 |
| `GET /v2/p/{page_id}?v={version_id}` | 页面查看器 |
| `GET /v2/req`、`/v2/req/{id}` | 需求单视图 |
| `POST /v2/req/{id}/pages/{page_id}/upload` | 上传需求版本 |
| `POST /v2/annotations` … | 批注增删改 |
| `GET/POST /v2/admin/systems`、`/apps`、`/menus` | 系统/端/菜单维护（菜单可从路由 JSON 导入） |
| `POST /v2/admin/snapshots` | 导入快照包（生成 release + 基线版本） |
| `GET /p2/{share_code}/...` | 需求单公开分享（目录 + 各页 diff 视图） |

## 8. 分期

- **一期（先做）**：系统/端/菜单维护与路由 JSON 导入；快照工具（html 与 screenshot 两种渲染器）与导入；页面时间线；需求单关联与上传；html DOM diff 与图片像素 diff 自动圈出；两个视图；整单分享链接。
- **二期**：人工批注编辑器；快照自动定时拉取；页面搜索增强；1.0 需求迁移到 2.0（把 1.0 的文档当作无菜单归属的"游离页面"）。
- **三期**：主入口切换，1.0 变为只读归档。

## 9. 已确认事项（2026-09-08）

1. 规模：页面上千；三个项目对应多个系统，每个系统多个后台/App。
2. App 为 Flutter，H5 是 **Flutter Web 编译产物**（canvas）→ 走截图渲染 + 像素 diff，一期覆盖。
3. 线上有版本号，基线按 release 管理。
4. 前端可以提供路由配置、测试账号、UAT 环境等一切所需。
5. 自动圈出：以自动 diff 为主，人工批注兜底（编辑器二期）。
6. 权限沿用 1.0。
7. 需求单状态：草稿 / 待上线 / 已上线于 x.y.z，一期不与 Jira 同步，手动标记；导入 release 快照时若需求填写的上线版本与之相同则自动标为已上线。
8. 快照工具默认跑在需求方能访问 UAT 的机器上，产出 zip 后在后台导入；若 UAT 公网可达也可以直接在服务器上跑。
9. 新增页面：PM 在需求里先把页面挂到菜单树并标"新增"，快照进来后按 route_key 对齐。
