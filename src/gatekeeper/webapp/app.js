// On every page: registers the app's service worker (sw.js, next to this file), which makes
// the pages install as an app (README "The app").
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register(new URL("sw.js", document.currentScript.src)).catch(() => {});
}
