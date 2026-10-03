// The phone app that used to live here installed a service worker. This replacement
// clears its offline cache and unregisters itself so the chart page loads normally.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.registration.unregister();
    for (const client of await self.clients.matchAll({ type: 'window' })) client.navigate(client.url);
  })());
});
