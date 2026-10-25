self.addEventListener("push", (event) => {
  let data = {};

  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {
      title: "Duo Stock Monitor",
      body: event.data?.text() || "Inventory update received"
    };
  }

  event.waitUntil(
    self.registration.showNotification(
      data.title || "Duo Stock Monitor",
      {
        body: data.body || "Inventory check completed",
        icon: "./icon.png",
        tag: "duo-stock",
        renotify: true,
        data: {
          url: data.url || "./"
        }
      }
    )
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  event.waitUntil(
    clients.openWindow(event.notification.data?.url || "./")
  );
});