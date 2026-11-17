/* 오프라인에서도 열리게 앱 파일을 기기에 담아 둔다.
 * 메모·일정은 여기가 아니라 IndexedDB 에 있다 — 이 캐시는 앱 코드뿐이다.
 * 다른 주소(AI 서버 등)로 가는 요청은 건드리지 않는다.
 * VERSION 과 FILES 는 build_mobile.py 가 채운다.
 */
const VERSION = "__BUILD__";
const CACHE = `memoinall-${VERSION}`;
const FILES = /*FILES*/["./", "index.html", "app.js", "exchange.js", "qrshare.js", "vendor/qrcode.js", "vendor/jsQR.js", "manifest.webmanifest"]/*END*/;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("memoinall-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const hit = await cache.match(req, { ignoreSearch: true });
      if (hit) return hit;
      try {
        return await fetch(req);
      } catch (err) {
        if (req.mode === "navigate") return (await cache.match("./")) || Response.error();
        throw err;
      }
    }),
  );
});
