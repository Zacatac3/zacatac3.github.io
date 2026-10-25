self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {body: event.data?.text()}; }
  const receivedAt = new Date().toISOString();
  let body = data.body || 'Inventory checked';
  // New payloads already include an Eastern timestamp with the correct DST offset.
  if (!data.checked_at) {
    const sentAt = new Date(data.sent_at || receivedAt);
    if (!Number.isNaN(sentAt.getTime())) {
      const eastern = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York', timeZoneName: 'short',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true
      });
      body += `\nChecked: ${eastern.format(sentAt)}`;
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
