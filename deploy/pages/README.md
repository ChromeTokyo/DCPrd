# Cloudflare Pages 反向代理

`dcprd.pages.dev` → `https://dcpm.ddns.net` 的反向代理。Pages 只放这个目录（Advanced Mode，`_worker.js` 接管全部请求）。

2026-09-21 起主域名切回 `dcpm.ddns.net`，pages.dev 成为历史域名：公开链接继续可用，后台页面由源站 301 回主域名（所以 Worker 不改写 Location）。

部署：

```bash
npx wrangler@4 pages deploy deploy/pages --project-name dcprd --branch main --commit-dirty=true
```

环境变量（Pages 项目设置 → Variables）：`ORIGIN`，默认 `https://dcpm.ddns.net`。

限制：经 Cloudflare 的请求体上限 100 MB；Workers 免费额度每天 10 万次请求。
