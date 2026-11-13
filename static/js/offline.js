/* Mofisi offline mode: the device half (server half: apps/core/offline.py).
 *
 *  - registers the service worker that keeps visited pages and the app's files;
 *  - keeps a queue on this device (IndexedDB) of what was saved while offline:
 *    till tickets and forms marked data-offline="queue";
 *  - sends the queue when the connection is back, each item with its own id
 *    (X-Offline-Key) so the server books it once even if it is sent twice;
 *  - shows the state in the header: offline, N waiting, problems to fix.
 *
 *  Public: window.MerpOffline = { enqueue, list, remove, sync, reachable, count }.
 */
(function () {
  'use strict';
  var DB = 'merp-offline', STORE = 'queue', VERSION = 1;
  var meta = function (n) { var m = document.querySelector('meta[name="' + n + '"]'); return m ? m.content : ''; };
  var USER = meta('merp-user');
  var listeners = [];

  // ── service worker ──
  if ('serviceWorker' in navigator && USER) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () { /* not fatal */ });
  }

  // ── IndexedDB ──
  function db() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error('no indexedDB')); return; }
      var req = indexedDB.open(DB, VERSION);
      req.onupgradeneeded = function () {
        var s = req.result.createObjectStore(STORE, { keyPath: 'id' });
        s.createIndex('user', 'user');
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }
  function tx(mode, fn) {
    return db().then(function (d) {
      return new Promise(function (resolve, reject) {
        var t = d.transaction(STORE, mode), s = t.objectStore(STORE), out = fn(s);
        t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); };
        t.onerror = function () { reject(t.error); };
      });
    });
  }
  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    return Date.now().toString(16) + Math.random().toString(16).slice(2) + Math.random().toString(16).slice(2);
  }
  function list() {
    return tx('readonly', function (s) { return s.getAll(); }).then(function (all) {
      return (all || []).filter(function (i) { return i.user === USER; })
        .sort(function (a, b) { return a.created < b.created ? -1 : 1; });
    }).catch(function () { return []; });
  }
  function put(item) { return tx('readwrite', function (s) { return s.put(item); }); }
  function remove(id) { return tx('readwrite', function (s) { return s.delete(id); }).then(changed); }
  function count() { return list().then(function (l) { return l.length; }); }

  /* item: {kind: 'till'|'form', url, json?, fields?, label, amount?} */
  function enqueue(item) {
    var full = Object.assign({ id: newId(), user: USER, created: new Date().toISOString(),
                               status: 'pending', error: '', attempts: 0 }, item);
    return put(full).then(function () { changed(); return full; });
  }

  // ── is the server reachable? (navigator.onLine is often wrong) ──
  var lastReachable = null;            // what the last check found (navigator.onLine misses a dead server)
  function reachable(ms) {
    if (navigator.onLine === false) { lastReachable = false; changed(); return Promise.resolve(false); }
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, ms || 2500);
    return fetch('/ping/?t=' + Date.now(), { cache: 'no-store', credentials: 'same-origin', signal: ctl && ctl.signal })
      .then(function (r) { clearTimeout(timer); note(r.ok); return r.ok; })
      .catch(function () { clearTimeout(timer); note(false); return false; });
  }
  function note(up) {
    var was = lastReachable;
    lastReachable = up;
    if (was !== up) changed();
  }

  // ── sending ──
  var syncing = false;
  function send(item) {
    var headers = { 'X-Offline-Sync': '1', 'X-Offline-Key': item.id, 'X-CSRFToken': meta('csrf-token'),
                    'X-Requested-With': 'fetch' };
    var body;
    if (item.kind === 'till') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(item.json);
    } else {
      body = new FormData();
      (item.fields || []).forEach(function (f) {
        if (f[0] === 'csrfmiddlewaretoken') return;          // today's token, not the saved one
        if (f[2]) body.append(f[0], f[1], f[2]);            // a file keeps its name
        else body.append(f[0], f[1]);
      });
      body.append('csrfmiddlewaretoken', meta('csrf-token'));
    }
    return fetch(item.url, { method: 'POST', body: body, headers: headers, credentials: 'same-origin' })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }); });
  }
  function sync() {
    if (syncing || !USER) return Promise.resolve({ sent: 0 });
    syncing = true;
    var sent = 0;
    return reachable().then(function (up) {
      if (!up) return { sent: 0, offline: true };
      return list().then(function (items) {
        var pending = items.filter(function (i) { return i.status === 'pending'; });
        var chain = Promise.resolve();
        var stop = false;
        pending.forEach(function (item) {
          chain = chain.then(function () {
            if (stop) return;
            return send(item).then(function (res) {
              if (res && res.ok) {
                sent += 1;
                notify((item.label || 'Saved item') + ' synced' + (res.reference ? ' as ' + res.reference : '') + '.', 'success');
                return remove(item.id);
              }
              item.attempts += 1;
              if (res && res.signin) { stop = true; notify('Sign in again to send what is waiting.', 'warning'); return; }
              if (res && res.permanent) {
                item.status = 'error'; item.error = res.error || 'Refused by the server.';
                notify((item.label || 'A saved item') + ' needs attention: ' + item.error, 'error');
                return put(item);
              }
              stop = true;                              // server busy or unreachable: later
            }).catch(function (err) {
              stop = true;
              if (window.console) console.warn('Offline sync stopped:', err);
            });
          });
        });
        return chain.then(function () { return { sent: sent }; });
      });
    }).finally(function () { syncing = false; changed(); });
  }

  // ── forms marked data-offline="queue" ──
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.getAttribute('data-offline') !== 'queue' || form.dataset.offlinePassed === '1') return;
    e.preventDefault();
    reachable().then(function (up) {
      if (up) { form.dataset.offlinePassed = '1'; form.submit(); return; }
      var fields = [];
      new FormData(form).forEach(function (v, k) {
        fields.push(v instanceof File ? [k, v, v.name] : [k, v]);
      });
      var label = form.getAttribute('data-offline-label') || document.title.replace(/ — Mofisi$/, '');
      enqueue({ kind: 'form', url: form.getAttribute('action') || window.location.pathname, fields: fields,
                label: label }).then(function () {
        notify(label + ' saved on this device. It will be sent when you are back online.', 'info');
        form.reset();
      });
    });
  }, true);

  // ── signing out: forget the pages kept for this login ──
  document.addEventListener('submit', function (e) {
    var a = (e.target && e.target.getAttribute('action')) || '';
    if (!/\/auth\/logout\/?$/.test(a)) return;
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage('clear-pages');
    }
  }, true);

  // ── header indicator ──
  function notify(message, kind) {
    if (typeof window.showToast === 'function') window.showToast(message, kind === 'error' ? 'error' : kind);
    else if (typeof window.posToast === 'function') window.posToast(message, kind);
  }
  function changed() {
    list().then(function (items) {
      var waiting = items.filter(function (i) { return i.status === 'pending'; }).length;
      var problems = items.filter(function (i) { return i.status === 'error'; }).length;
      var offline = navigator.onLine === false || lastReachable === false;
      document.querySelectorAll('[data-offline-indicator]').forEach(function (box) {
        box.hidden = !(offline || waiting || problems);
        box.classList.toggle('is-offline', offline);
        box.classList.toggle('has-problems', problems > 0);
        var label = box.querySelector('[data-offline-label]');
        if (label) label.textContent = offline ? (waiting ? 'Offline · ' + waiting + ' waiting' : 'Offline')
          : problems ? problems + ' to fix' : waiting + ' to sync';
        var count = box.querySelector('[data-offline-count]');
        if (count) { count.textContent = String(waiting + problems); count.hidden = !(waiting + problems); }
      });
      document.querySelectorAll('[data-offline-list]').forEach(function (ul) { draw(ul, items); });
      listeners.forEach(function (fn) { try { fn(items, offline); } catch (err) { /* ignore */ } });
    });
  }
  function draw(ul, items) {
    ul.textContent = '';
    if (!items.length) {
      var empty = document.createElement('li');
      empty.className = 'px-3 py-3 text-sm text-ink-muted';
      empty.textContent = 'Nothing waiting. Everything is synced.';
      ul.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'flex items-start gap-2 px-3 py-2 text-sm';
      var text = document.createElement('div');
      text.className = 'min-w-0 flex-1';
      var t = document.createElement('div');
      t.className = 'font-semibold text-ink';
      t.textContent = (item.label || 'Saved item') + (item.amount ? ' · ' + item.amount : '');
      var s = document.createElement('div');
      s.className = 'text-xs ' + (item.status === 'error' ? 'text-danger' : 'text-ink-muted');
      s.textContent = item.status === 'error' ? item.error : 'Saved ' + new Date(item.created).toLocaleString() + ' · waiting';
      text.appendChild(t); text.appendChild(s);
      li.appendChild(text);
      if (item.status === 'error') {
        var retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'btn btn-ghost btn-sm'; retry.textContent = 'Retry';
        retry.addEventListener('click', function () { item.status = 'pending'; item.error = ''; put(item).then(sync); });
        var drop = document.createElement('button');
        drop.type = 'button'; drop.className = 'btn btn-ghost btn-sm text-danger'; drop.textContent = 'Discard';
        drop.addEventListener('click', function () {
          if (window.confirm('Discard "' + (item.label || 'this item') + '"? It was never saved on the server.')) remove(item.id);
        });
        li.appendChild(retry); li.appendChild(drop);
      }
      ul.appendChild(li);
    });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-offline-sync]');
    if (b) { e.preventDefault(); sync(); }
  });

  window.addEventListener('online', function () { changed(); sync(); });
  window.addEventListener('offline', changed);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) sync(); });
  setInterval(function () {
    count().then(function (n) { if (n) sync(); else if (lastReachable === false) reachable(); });
  }, 30000);

  window.MerpOffline = { enqueue: enqueue, list: list, remove: remove, sync: sync, reachable: reachable,
                         count: count, onChange: function (fn) { listeners.push(fn); changed(); } };
  changed();
  if (USER) setTimeout(sync, 1500);
})();
