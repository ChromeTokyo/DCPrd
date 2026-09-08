# 给云桌面里 AI 助手的任务提示词（复制后按需改括号内容）

你在一台能访问我们测试环境（UAT）的机器上，帮我为「DCPrd 2.0 需求管理系统」生成一个端的页面快照包。请按下面步骤做，遇到需要我操作的地方（比如登录）停下来告诉我。

1. 从仓库获取工具：`git clone https://github.com/ChromeTokyo/DCPrd.git`，工具在 `DCPrd/tools/snap/`，先读一遍 `README.md`。
2. 准备 Python 环境：`python3 -m venv .snap && . .snap/bin/activate && pip install playwright && python -m playwright install chromium`（Windows 用 `.snap\Scripts\activate`）。
3. 从前端项目【前端仓库路径：____】里找到路由/菜单配置（通常在 `src/router/` 或 `src/menu/`，Vue Router / React Router 的 routes 数组），把它导出成纯 JSON 文件 `routes.json`：保留每项的 `path`、`meta.title`（或 `name`/`title`）、`children`、`hidden`，去掉组件引用等无法序列化的字段。如果路由是动态从接口拿的，用测试账号登录后调用那个菜单接口，把返回的 JSON 存成 `routes.json`。
4. 复制 `example-config.json` 为 `【端标识，如 eb-ops-admin】.json`，填写：
   - `app`：`【端标识】`（要和 DCPrd 里配置的一致）
   - `release`：`【本次线上版本号】`
   - `base_url`：`【UAT 地址】`
   - `kind`：Web 后台填 `web`；Flutter Web 的 App 填 `flutter_web`（这时用 `example-config-flutter.json`，视口 390×844，`hash_routing` 按实际路由方式填）
   - `routes`：`./routes.json`
   - `mask`：把列表表体、时间等动态区域的 CSS 选择器填进去（Element UI 是 `.el-table__body`，Ant Design 是 `.ant-table-tbody`）
5. 先跑 `python snap.py --config 【配置】.json --login`，浏览器弹出后叫我登录；我登录完你按回车让它保存登录态。
6. 先小范围试抓：`python snap.py --config 【配置】.json --limit 3 --headful`，解压检查 zip 里的 html 打开是否正常（样式、图片齐全）/ png 是否完整。有问题调整 `wait`、`mask`、`remove` 后再试。
7. 全量抓取：`python snap.py --config 【配置】.json`，把生成的 `snapshot-*.zip` 和 `report.json` 里失败页面的列表告诉我。
8. 把 zip 交给我，我会在 DCPrd 2.0 的「系统与端 → 导入 release 快照」上传；不要把登录态文件 `state.json` 提交到任何仓库。
