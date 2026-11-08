// The Visits page's switch for the app's notifications on this phone (webpush.py). It shows
// only where the browser can take them: over HTTPS, with the app's service worker running
// (on an iPhone, in the installed app).

(async () => {
  const box = document.getElementById("push");
  if (!box || !("serviceWorker" in navigator) || !("PushManager" in window)) return;
  const reg = await navigator.serviceWorker.ready;
  const key = Uint8Array.from(
    atob(box.dataset.key.replace(/-/g, "+").replace(/_/g, "/")),
    (c) => c.charCodeAt(0),
  );

  async function post(action, body) {
    const r = await fetch("push/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": box.dataset.token },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
  }

  function say(text, ...buttons) {
    box.replaceChildren(text, ...buttons.flatMap((b) => [" ", b]));
    box.hidden = false;
  }

  function button(text, act) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.onclick = () => act().then(show, (e) => show(e.message || String(e)));
    return b;
  }

  async function show(note = "") {
    const sub = await reg.pushManager.getSubscription();
    const lead = note ? note + " " : "";
    if (Notification.permission === "denied") {
      say(lead + "Notifications are blocked for this app: allow them in the phone's settings.");
    } else if (!sub) {
      say(lead + "Notifications on this phone are off.", button("Turn on", turnOn));
    } else {
      say(
        lead + "Notifications on this phone are on.",
        button("Send a test", () => post("test", { endpoint: sub.endpoint }).then(() => "Sent.")),
        button("Turn off", () => turnOff(sub)),
      );
    }
  }

  async function turnOn() {
    if ((await Notification.requestPermission()) !== "granted") return "";
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    await post("subscribe", sub.toJSON());
    return "";
  }

  async function turnOff(sub) {
    await post("unsubscribe", { endpoint: sub.endpoint });
    await sub.unsubscribe();
    return "";
  }

  // A subscription made with another key (Gatekeeper's was made again) can't be sent to.
  let sub = await reg.pushManager.getSubscription();
  const made = sub && sub.options.applicationServerKey;
  if (made && new Uint8Array(made).join() !== key.join()) {
    await sub.unsubscribe();
    sub = null;
  }
  // Told again at each visit, in case the server lost it (it's kept once).
  if (sub) post("subscribe", sub.toJSON()).catch(() => {});
  show();
})();
