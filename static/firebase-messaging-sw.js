importScripts("https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js");

const app = firebase.initializeApp({
    apiKey: "AIzaSyCWRfI4zUk27QGPbO6crBwIuRr5xpoS0xk",
    authDomain: "church-app-21147.firebaseapp.com",
    projectId: "church-app-21147",
    storageBucket: "church-app-21147.firebasestorage.app",
    messagingSenderId: "681140747186",
    appId: "1:681140747186:web:c5c0957b553e2361e902ab"
});

const messaging = firebase.messaging(app);

messaging.onBackgroundMessage(function(payload) {
    console.log("[firebase-messaging-sw.js] Background message:", payload);
    self.registration.showNotification(
        payload.notification.title,
        {
            body:  payload.notification.body,
            icon:  "/static/icons/icon-192.png",
            badge: "/static/icons/icon-192.png",
            vibrate: [200, 100, 200]
        }
    );
});