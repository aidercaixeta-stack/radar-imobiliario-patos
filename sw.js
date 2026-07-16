const CACHE = "radar-patos-v15";
const ASSETS = [
  "./",
  "index.html",
  "styles.css",
  "v14.css",
  "v15.css",
  "app.js",
  "interactions-v14.js",
  "manifest.webmanifest",
  "data/imoveis.json",
  "data/meta.json",
  "data/fontes/leilaoimovel_index.json",
  "data/fontes/leilaoimovel_meta.json",
  "assets/icon-192.png",
  "assets/icon-512.png"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then(response => {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
