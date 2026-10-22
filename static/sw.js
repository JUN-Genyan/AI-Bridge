// 装成 app 用的 service worker：页面、接口、附件一律直连服务器，从不拿缓存顶替（会议内容必须是最新的）。
// 只缓存一张"会议室没开"的提示页：服务器没开时打开 app，看到的是它而不是浏览器的报错页。
const V = 'rt-v2', OFF = '/static/offline.html';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll([OFF, '/static/icon-192.png'])));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(OFF)));
});
