// Cloudflare Worker: forwards Telegram Bot API calls for servers that cannot reach api.telegram.org.
// Deploy: dash.cloudflare.com -> Workers & Pages -> Create -> Worker -> paste this file -> Deploy.
// Then set TELEGRAM_API_URL=https://<worker-name>.<account>.workers.dev in .env.
//
// Forwarded: Bot API (/bot<token>/<method>, /file/bot<token>/<path>) and public channel previews
// (/s/<channel> -> https://t.me/s/<channel>) used by the scraper. Optionally set the ALLOWED_BOT_ID
// variable — the digits before ":" in each token, comma-separated — so the worker serves only your bots.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && /^\/s\/[\w]{3,64}\/?$/.test(url.pathname)) {
      return fetch("https://t.me" + url.pathname + url.search, { headers: { "User-Agent": request.headers.get("User-Agent") || "Mozilla/5.0" } });
    }
    const match = url.pathname.match(/^\/(?:file\/)?bot(\d+):[\w-]+\//);
    if (!match) {
      return new Response("Not found", { status: 404 });
    }
    const allowed = (env.ALLOWED_BOT_ID || "").split(",").map((id) => id.trim()).filter(Boolean);
    if (allowed.length && !allowed.includes(match[1])) {
      return new Response("Forbidden", { status: 403 });
    }
    const target = "https://api.telegram.org" + url.pathname + url.search;
    const headers = new Headers(request.headers);
    headers.delete("host");
    return fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "follow",
    });
  },
};
