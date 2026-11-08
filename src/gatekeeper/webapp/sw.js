// Gatekeeper's web app (README "The app"). Nothing is kept on the phone: visits, faces and
// the camera stay on the server, so every page is fetched from it. When the server can't be
// reached (away from home without a way in), a page says so instead of the browser's error.

const STYLE = `body { font: 16px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 960px;
  padding: 0 16px; color: #1b1b1f; background: #fafafa; }
@media (prefers-color-scheme: dark) { body { background: #17171a; color: #e8e8ea; } }`;

const UNREACHABLE = `<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Gatekeeper</title><style>${STYLE}</style></head><body>
<h1>Gatekeeper can't be reached</h1>
<p>At home, check that the phone is on the home Wi-Fi. Away from home, connect the way you
reach the server from outside (a VPN, for now).</p>
<p><button onclick="location.reload()">Try again</button></p>
</body></html>`;

// The page a notification's 911 button opens, made here so it opens even when the server
// can't be reached: the phone's own links, to call 911 or to text it (EMERGENCY_BUTTONS).
const EMERGENCY = `<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>911</title><style>${STYLE}
a.call { display: block; margin: 16px 0; padding: 18px; border-radius: 10px; font-size: 1.3em;
  font-weight: 600; text-align: center; color: #fff; background: #c62828; text-decoration: none; }
</style></head><body>
<h1>911</h1>
<div id=links></div>
<p id=text hidden>Text 911 opens your messages with the address and what happened: you still
tap send.</p>
<script>
let links = [];
try { links = JSON.parse(decodeURIComponent(location.hash.slice(1))); } catch (e) {}
// Only the phone's call and text links: anything else could run on this page.
links = (Array.isArray(links) ? links : []).filter(
  (l) => l && typeof l.url === "string" && /^(tel|sms):/.test(l.url));
if (!links.length) links = [{ title: "Call 911", url: "tel:911" }];
for (const l of links) {
  const a = document.createElement("a");
  a.className = "call";
  a.href = l.url;
  a.textContent = String(l.title);
  document.getElementById("links").append(a);
}
document.getElementById("text").hidden = !links.some((l) => l.url.startsWith("sms:"));
</script>
</body></html>`;

const html = (body, status) =>
  new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// The app's notifications (webpush.py): the visitor's snapshot (fetched from the server),
// a visit's next one replaces it (its tag), and tapping one opens its page (Live). Its
// buttons, as many as the phone shows: 911 first (one button, for its page), then the
// quick replies.
self.addEventListener("push", (event) => {
  const n = event.data ? event.data.json() : { title: "Gatekeeper" };
  const scope = self.registration.scope;
  const most =
    typeof Notification !== "undefined" && typeof Notification.maxActions === "number"
      ? Notification.maxActions
      : 2;
  const links = n.links || [];
  const call = links.length > 1 ? "Call or text 911" : links.length ? links[0].title : "";
  const buttons = [...(call ? [{ title: call, links }] : []), ...(n.replies || [])].slice(0, most);
  event.waitUntil(
    self.registration.showNotification(n.title, {
      body: n.body || "",
      icon: "icon-192.png",
      badge: "badge-96.png",
      image: n.image ? new URL(n.image, scope).href : undefined,
      tag: n.tag || undefined,
      renotify: Boolean(n.tag),
      silent: Boolean(n.silent),
      actions: buttons.map((b, i) => ({ action: String(i), title: b.title })),
      data: { url: new URL(n.url || "./", scope).href, buttons, tag: n.tag },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  const { url, buttons = [], tag } = event.notification.data;
  const button = event.action === "" ? undefined : buttons[Number(event.action)];
  event.notification.close();
  if (button && button.links) {
    const page = new URL("emergency", self.registration.scope);
    page.hash = encodeURIComponent(JSON.stringify(button.links));
    event.waitUntil(self.clients.openWindow(page.href));
  } else if (button && button.reply) {
    event.waitUntil(reply(button, tag));
  } else {
    event.waitUntil(
      self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((open) => {
        const same = open.find((c) => c.url === url);
        return same ? same.focus() : self.clients.openWindow(url);
      }),
    );
  }
});

// A quick reply: the button's signed text, to the server (quick_reply.py says it at the
// door). If it doesn't take it, a notification says the reply wasn't said.
async function reply(button, tag) {
  let why = "Gatekeeper couldn't be reached.";
  try {
    const r = await fetch(new URL("push/reply", self.registration.scope), {
      method: "POST",
      body: button.reply,
    });
    if (r.ok) return;
    why = `Gatekeeper answered ${r.status}.`;
  } catch (e) {
    // unreachable: said below
  }
  await self.registration.showNotification(`"${button.title}" wasn't said at the door`, {
    body: why,
    icon: "icon-192.png",
    badge: "badge-96.png",
    tag: tag || undefined,
  });
}

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  const path = new URL(event.request.url).pathname;
  if (path === new URL("emergency", self.registration.scope).pathname) {
    event.respondWith(html(EMERGENCY, 200));
    return;
  }
  event.respondWith(fetch(event.request).catch(() => html(UNREACHABLE, 503)));
});
