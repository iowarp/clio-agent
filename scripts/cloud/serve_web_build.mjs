// Serve a production web build and forward /v1 (SSE included) to the running agent,
// the same-origin shape `clio web` gives, so UI checks can measure what ships.
//
//   (cd external/gact-tui/web && npx vite build --outDir ../../../.cloud-run/web-build)
//   node scripts/cloud/serve_web_build.mjs [DIST] [PORT]   # defaults .cloud-run/web-build, 4174
//   node scripts/cloud/ui_check.mjs --ui http://127.0.0.1:4174/ --agent http://127.0.0.1:4174 …
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
const ROOT = path.resolve(process.argv[2] || ".cloud-run/web-build");
const PORT = Number(process.argv[3] || 4174);
const AGENT_PORT = Number(process.env.AGENT_PORT || 8787);
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json", ".wasm": "application/wasm", ".png": "image/png", ".woff2": "font/woff2" };
http.createServer((req, res) => {
  if (req.url.startsWith("/v1/") || req.url === "/v1") {
    const up = http.request({ host: "127.0.0.1", port: AGENT_PORT, path: req.url, method: req.method, headers: req.headers }, (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
    up.on("error", () => { res.writeHead(502); res.end(); });
    req.pipe(up);
    return;
  }
  let file = path.join(ROOT, decodeURIComponent(req.url.split("?")[0]));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(ROOT, "index.html");
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}).listen(PORT, "127.0.0.1", () => console.log(`web build ${ROOT} on ${PORT}`));
