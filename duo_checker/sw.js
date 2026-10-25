self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {body: event.data?.text()}; }
  const receivedAt = new Date().toISOString();
  let body = data.body || 'Inventory checked';
  // New payloads already include the inventory check time, formatted in EST.
  if (!data.checked_at) {
    const sentAt = new Date(data.sent_at || receivedAt);
    if (!Number.isNaN(sentAt.getTime())) {
      const est = new Date(sentAt.getTime() - 5 * 60 * 60 * 1000);
      body += `\nChecked: ${est.toISOString().slice(0, 19).replace('T', ' ')} EST`;
    }
  }
  event.waitUntil(self.registration.showNotification(data.title || 'Duo Monitor', {
    body, data: {url: data.url || './', sent_at: data.sent_at, received_at: receivedAt},
    // Keep stock alerts and four-hour status checks separately in Notification Center.
    tag: `duo-${data.check_id || Date.now()}`
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data?.url || './'));
});
