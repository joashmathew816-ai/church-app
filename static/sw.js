importScripts("https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js");

// ------------------------------------------------
// FIREBASE INIT
// Replace these with your actual config values
// ------------------------------------------------
const firebaseApp = firebase.initializeApp({
    apiKey: "AIzaSyCWRfI4zUk27QGPbO6crBwIuRr5xpoS0xk",
    authDomain: "church-app-21147.firebaseapp.com",
    projectId: "church-app-21147",
    storageBucket: "church-app-21147.firebasestorage.app",
    messagingSenderId: "681140747186",
    appId: "1:681140747186:web:c5c0957b553e2361e902ab"
});

const messaging = firebase.messaging(firebaseApp);

// ------------------------------------------------
// BACKGROUND PUSH NOTIFICATIONS
// ------------------------------------------------
messaging.onBackgroundMessage(function(payload) {
    console.log("[sw.js] Background message received:", payload);
    const title   = payload.notification.title || "Church App";
    const options = {
        body:    payload.notification.body || "",
        icon:    "/static/icons/icon-192.png",
        badge:   "/static/icons/icon-192.png",
        vibrate: [200, 100, 200],
        data:    payload.data || {}
    };
    self.registration.showNotification(title, options);
});

// ------------------------------------------------
// INSTALL — skip waiting immediately
// ------------------------------------------------
self.addEventListener("install", function(event) {
    console.log("[sw.js] Installing...");
    self.skipWaiting();
});

// ------------------------------------------------
// ACTIVATE — take control immediately
// ------------------------------------------------
self.addEventListener("activate", function(event) {
    console.log("[sw.js] Activating...");
    event.waitUntil(clients.claim());
});

// ------------------------------------------------
// FETCH — serve from network, fallback to offline
// ------------------------------------------------
self.addEventListener("fetch", function(event) {
    if (event.request.mode === "navigate") {
        event.respondWith(
            fetch(event.request).catch(function() {
                return caches.match("/offline") ||
                       new Response("You are offline.", {
                           status: 503,
                           headers: { "Content-Type": "text/plain" }
                       });
            })
        );
    }
});