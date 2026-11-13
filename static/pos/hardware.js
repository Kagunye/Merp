/* Mofisi till hardware: receipt printers and cash drawers on the shop LAN.
 *
 * A browser cannot open a raw socket to a printer, so each printer says how
 * it is reached (apps/pos/hardware.py builds the ESC/POS bytes):
 *   BRIDGE  POST {host, port, data} to the Mofisi bridge on this till PC
 *           (tools/mofisi-bridge), which writes them to <LAN IP>:9100;
 *   EPSON   POST ePOS-Print XML (<command> hex) to the printer's own web service;
 *   STAR    POST StarWebPRNT XML to the printer's own web service;
 *   SERVER  ask the server to send them (a server on the shop's LAN).
 * Each till PC remembers which printer it uses (localStorage "merp.printer").
 * Every drawer opening is logged on the server (and queued when offline).
 *
 * window.MerpHardware = { printers, current, choose, openDrawer, printReceipt,
 *                         afterSale, test, ready }
 */
(function () {
  'use strict';
  var KEY = 'merp.printer';
  var meta = function (n) { var m = document.querySelector('meta[name="' + n + '"]'); return m ? m.content : ''; };
  var printers = [];
  var loaded;

  function load() {
    if (loaded) return loaded;
    var inline = document.getElementById('pos-printers');
    if (inline) {
      try { printers = JSON.parse(inline.textContent) || []; } catch (e) { printers = []; }
      loaded = Promise.resolve(printers);
      return loaded;
    }
    loaded = fetch('/pos/hardware/config/', { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.ok ? r.json() : { printers: [] }; })
      .then(function (d) { printers = d.printers || []; return printers; })
      .catch(function () { printers = []; return printers; });
    return loaded;
  }
  function current() {
    var id = null;
    try { id = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
    return printers.filter(function (p) { return p.id === id; })[0] || printers[0] || null;
  }
  function choose(id) { try { localStorage.setItem(KEY, id); } catch (e) { /* ignore */ } }

  function hex(b64) {
    var bin = atob(b64), out = '';
    for (var i = 0; i < bin.length; i++) out += ('0' + bin.charCodeAt(i).toString(16)).slice(-2);
    return out;
  }
  function timeout(ms) {
    var ctl = window.AbortController ? new AbortController() : null;
    if (ctl) setTimeout(function () { ctl.abort(); }, ms);
    return ctl ? ctl.signal : undefined;
  }
  function esc(s) { return String(s).replace(/[<>&"]/g, function (c) { return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]; }); }

  // ── carry bytes to a printer ──
  function send(p, b64, extra) {
    extra = extra || {};
    if (p.connection === 'BRIDGE') {
      return fetch(p.bridge + '/send', { method: 'POST', signal: timeout(8000), headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host: p.host, port: p.port, data: b64 }) })
        .then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); })
        .then(function (d) { if (!d.ok) throw new Error(d.error || 'The bridge could not reach the printer.'); });
    }
    if (p.connection === 'EPSON') {
      var scheme = (p.port === 443 || p.port === 8043) ? 'https' : 'http';
      var xml = '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">' +
        '<s:Body><epos-print xmlns="http://www.epson-pos.com/schemas/2011/03/epos-print"><command>' + hex(b64) +
        '</command></epos-print></s:Body></s:Envelope>';
      return fetch(scheme + '://' + p.host + ':' + p.port + '/cgi-bin/epos/service.cgi?devid=local_printer&timeout=10000',
        { method: 'POST', signal: timeout(12000), headers: { 'Content-Type': 'text/xml; charset=utf-8' }, body: xml })
        .then(function (r) { return r.text(); })
        .then(function (t) { if (!/success="true"/.test(t)) throw new Error('The Epson printer refused the job.'); });
    }
    if (p.connection === 'STAR') {
      var body = '<root>' + (extra.text ? '<text>' + esc(extra.text) + '\n\n</text><cutpaper feed="true" type="partial"/>' : '') +
        (extra.kick ? '<peripheral channel="1" on="200" off="200"/>' : '') + '</root>';
      return fetch('http://' + p.host + (p.port && p.port !== 80 ? ':' + p.port : '') + '/StarWebPRNT/SendMessage',
        { method: 'POST', signal: timeout(12000), headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'request=' + encodeURIComponent(body) })
        .then(function (r) { return r.text(); })
        .then(function (t) { if (!/success\s*=\s*"?true/i.test(t) && !/<success>true/i.test(t)) throw new Error('The Star printer refused the job.'); });
    }
    if (p.connection === 'SERVER') {
      var form = new URLSearchParams({ printer: p.id, action: extra.action || 'drawer', sale: extra.sale || '', kick: extra.kick ? '1' : '0' });
      return fetch('/pos/hardware/send/', { method: 'POST', credentials: 'same-origin', body: form,
        headers: { 'X-CSRFToken': meta('csrf-token'), 'X-Requested-With': 'fetch' } })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (!d.ok) throw new Error(d.error || 'The server could not reach the printer.'); });
    }
    return Promise.reject(new Error('Unknown printer connection.'));
  }

  // ── log every drawer opening (queued when the server cannot be reached) ──
  function log(p, reason, opts, ok, error) {
    var fields = { printer: p ? p.id : '', reason: reason, sale: opts.sale || '', note: opts.note || '',
                   ok: ok ? '1' : '0', error: error || '' };
    return fetch('/pos/hardware/log/', { method: 'POST', credentials: 'same-origin', body: new URLSearchParams(fields),
      headers: { 'X-CSRFToken': meta('csrf-token'), 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.json(); })
      .catch(function () {
        if (window.MerpOffline) {
          return MerpOffline.enqueue({ kind: 'form', url: '/pos/hardware/log/', label: 'Drawer opened (' + reason + ')',
            fields: Object.keys(fields).map(function (k) { return [k, fields[k]]; }) });
        }
      });
  }

  function openDrawer(opts) {
    opts = opts || {};
    var reason = opts.reason || 'NO_SALE';
    return load().then(function () {
      var p = current();
      if (!p) return Promise.reject(new Error('No printer or cash drawer is set up for this till.'));
      return send(p, p.drawer_b64, { kick: true, action: 'drawer' })
        .then(function () { log(p, reason, opts, true); return true; },
              function (err) { log(p, reason, opts, false, err.message); throw err; });
    });
  }

  function printReceipt(saleId, opts) {
    opts = opts || {};
    return load().then(function () {
      var p = current();
      if (!p) return Promise.reject(new Error('No receipt printer is set up for this till.'));
      if (p.connection === 'SERVER') return send(p, null, { action: 'receipt', sale: saleId, kick: !!opts.kick });
      return fetch('/pos/hardware/receipt/' + saleId + '/?printer=' + encodeURIComponent(p.id) + (opts.kick ? '&kick=1' : ''),
        { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
        .then(function (r) { if (!r.ok) throw new Error('Could not build the receipt.'); return r.json(); })
        .then(function (d) { return send(p, d.data, { text: d.text, kick: !!opts.kick }); });
    });
  }

  /* After a sale: print the receipt (with the drawer pulse for cash, in one
     job) and log the drawer opening. Resolves to {printed, drawer, error}. */
  function afterSale(saleId, cash, reason) {
    return load().then(function () {
      var p = current();
      if (!p) return { printed: false, drawer: false, error: '' };
      var kick = !!(cash && p.drawer_on_cash);
      var job = p.print ? printReceipt(saleId, { kick: kick })
        : (kick ? send(p, p.drawer_b64, { kick: true, action: 'drawer' }) : Promise.resolve());
      return job.then(function () {
        if (kick) log(p, reason || 'SALE', { sale: saleId }, true);
        return { printed: p.print, drawer: kick, error: '' };
      }, function (err) {
        if (kick) log(p, reason || 'SALE', { sale: saleId }, false, err.message);
        return { printed: false, drawer: false, error: err.message };
      });
    });
  }

  function test(id, what) {
    return load().then(function () {
      var p = printers.filter(function (x) { return x.id === id; })[0];
      if (!p) throw new Error('That printer is not switched on.');
      if (what === 'drawer') {
        return send(p, p.drawer_b64, { kick: true, action: 'drawer' }).then(function () { log(p, 'TEST', {}, true); });
      }
      // a short test page: ESC @, centred text, feed, cut
      var lines = '\x1b@\x1ba\x01Mofisi printer test\n' + p.name + '\n' + p.host + ':' + p.port + '\n' + new Date().toLocaleString() + '\n\n\n\x1dV\x42\x03';
      return send(p, btoa(unescape(encodeURIComponent(lines))), { text: 'Mofisi printer test\n' + p.name, action: 'test' });
    });
  }

  window.MerpHardware = { load: load, get printers() { return printers; }, current: current, choose: choose,
                          openDrawer: openDrawer, printReceipt: printReceipt, afterSale: afterSale, test: test };
})();
