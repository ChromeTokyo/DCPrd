# Cloudflare Pages 反向代理

`dcprd.pages.dev` → `https://dcpm.ddns.net`。Pages 只放这个目录（Advanced Mode，`_worker.js` 接管全部请求）。

部署：

```bash
npx wrangler@4 pages deploy deploy/pages --project-name dcprd --branch main --commit-dirty=true
```

环境变量（Pages 项目设置 → Variables）：`ORIGIN`，默认 `https://dcpm.ddns.net`。

限制：经 Cloudflare 的请求体上限 100 MB；Workers 免费额度每天 10 万次请求。
