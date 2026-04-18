const CACHE_NAME = "church-app-v1";

self.addEventListener("install", function(event) {
  console.log("Service worker installing...");
  self.skipWaiting();
});

self.addEventListener("activate", function(event) {
  console.log("Service worker activating...");
  event.waitUntil(clients.claim());
});

self.addEventListener("fetch", function(event) {
  event.respondWith(
    fetch(event.request).catch(function() {
      return caches.match(event.request);
    })
  );
});