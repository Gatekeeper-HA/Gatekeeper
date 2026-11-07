// Gatekeeper's web app (README "The app"). Nothing is kept on the phone: visits, faces and
// the camera stay on the server, so every page is fetched from it. When the server can't be
// reached (away from home without a way in), a page says so instead of the browser's error.

const UNREACHABLE = `<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Gatekeeper</title><style>
body { font: 16px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 960px;
  padding: 0 16px; color: #1b1b1f; background: #fafafa; }
@media (prefers-color-scheme: dark) { body { background: #17171a; color: #e8e8ea; } }
</style></head><body>
<h1>Gatekeeper can't be reached</h1>
<p>At home, check that the phone is on the home Wi-Fi. Away from home, connect the way you
reach the server from outside (a VPN, for now).</p>
<p><button onclick="location.reload()">Try again</button></p>
</body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () =>
        new Response(UNREACHABLE, {
          status: 503,
          headers: { "Content-Type": "text/html; charset=utf-8" },
        }),
    ),
  );
});
