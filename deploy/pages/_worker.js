// DCPrd 在 Cloudflare Pages 上的反向代理：把 dcprd.pages.dev 的全部请求转给源站，响应原样返回。
// 源站地址通过 Pages 环境变量 ORIGIN 配置（默认 https://dcpm.ddns.net）。
const HOP_BY_HOP = ["connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"];

export default {
  async fetch(request, env) {
    const origin = (env.ORIGIN || "https://dcpm.ddns.net").replace(/\/+$/, "");
    const inUrl = new URL(request.url);
    const target = origin + inUrl.pathname + inUrl.search;

    const headers = new Headers(request.headers);
    for (const h of HOP_BY_HOP) headers.delete(h);
    headers.set("X-Forwarded-Host", inUrl.host);
    headers.set("X-Original-Host", inUrl.host); // Caddy 会覆盖 X-Forwarded-Host，源站以此为准
    headers.set("X-Forwarded-Proto", "https");
    const ip = request.headers.get("CF-Connecting-IP");
    if (ip) headers.set("X-Forwarded-For", ip);
    // 源站 Caddy 按 Host 匹配站点，Host 必须是源站域名（由 fetch 按 target 自动设置）
    headers.delete("host");

    const init = {
      method: request.method,
      headers,
      redirect: "manual", // 30x 原样透传，避免 Worker 自己跟随跳转
    };
    if (!["GET", "HEAD"].includes(request.method)) init.body = request.body;

    let resp;
    try {
      resp = await fetch(target, init);
    } catch (e) {
      return new Response("源站暂时不可用，请稍后再试。\n" + (e && e.message ? e.message : ""), {
        status: 502,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    const out = new Headers(resp.headers);
    for (const h of HOP_BY_HOP) out.delete(h);
    // 源站若返回指向源站域名的绝对跳转，改写回当前域名
    const loc = out.get("location");
    if (loc && loc.startsWith(origin)) out.set("location", inUrl.origin + loc.slice(origin.length));
    return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: out });
  },
};
