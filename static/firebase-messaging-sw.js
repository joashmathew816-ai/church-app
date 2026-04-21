importScripts("https://www.gstatic.com/firebasejs/10.7.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.7.0/firebase-messaging-compat.js");

firebase.initializeApp({
  apiKey: "AIzaSyCWRfI4zUk27QGPbO6crBwIuRr5xpoS0xk",
  authDomain: "church-app-21147.firebaseapp.com",
  projectId: "church-app-21147",
  storageBucket: "church-app-21147.firebasestorage.app",
  messagingSenderId: "681140747186",
  appId: "1:681140747186:web:c5c0957b553e2361e902ab"
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage(function(payload) {
  console.log("Background message received:", payload);

  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png"
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});