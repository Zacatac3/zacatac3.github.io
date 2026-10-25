self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {body: event.data?.text()}; }
  const receivedAt = new Date().toISOString();
  const body = `${data.body || 'Inventory checked'}\nSent: ${data.sent_at || 'unknown'}\nSW received: ${receivedAt}`;
  event.waitUntil(self.registration.showNotification(data.title || 'Duo Monitor', {
    body, data: {url: data.url || './', sent_at: data.sent_at, received_at: receivedAt},
    // Unique tag so every check creates a distinct notification.
    tag: `duo-${data.check_id || Date.now()}`
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data?.url || './'));
});
