// Service Worker — Bloomberg MKT Push Notifications
const CACHE_NAME = 'mkt-agent-v1';

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(clients.claim());
});

// Recibir mensaje del dashboard para mostrar notificación
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'NOTIFY') {
    const { title, body, tag, urgency } = e.data;
    const icon = urgency === 'critical' ? '/icon-red.png' : '/icon-yellow.png';
    self.registration.showNotification(title, {
      body: body,
      tag: tag,         // evita duplicados — misma tag = reemplaza la anterior
      icon: icon,
      badge: icon,
      vibrate: urgency === 'critical' ? [200, 100, 200, 100, 200] : [200, 100, 200],
      requireInteraction: urgency === 'critical', // rojo se queda hasta que la tocas
      data: { urgency }
    });
  }
});

// Al tocar la notificación, abrir/enfocar el dashboard
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin)) {
          return c.focus();
        }
      }
      return clients.openWindow('/');
    })
  );
});
