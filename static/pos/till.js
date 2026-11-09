/* The till (templates/pos/terminal.html).
 *
 * Display and input only: the server prices the ticket. This script keeps the
 * lines the cashier picked, shows an estimate of the bill, and on "Complete
 * sale" writes them into the existing POST contract — qty_<product pk> and
 * disc_<product pk> hidden fields beside ticket_token — and submits the form.
 * Nothing it computes reaches the ledger.
 */
(function () {
  'use strict';
  var root = document.getElementById('pos');
  if (!root) return;

  // ── data ──────────────────────────────────────────────────────────────
  function json(id, fallback) {
    var el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : fallback; } catch (e) { return fallback; }
  }
  var PRODUCTS = json('pos-products', []);
  var CATEGORIES = json('pos-categories', []);
  var SERVER_TICKET = json('pos-ticket', {});
  var BOMS = json('pos-boms', []);
  var ACCOUNTS = json('pos-accounts', {});
  var ACCOUNT_DEFAULTS = json('pos-account-defaults', {});
  var byId = {};
  PRODUCTS.forEach(function (p) { byId[p.id] = p; });
  var catById = {};
  CATEGORIES.forEach(function (c) { catById[c.id] = c; });

  var cfg = root.dataset;
  var SUPERVISOR = cfg.supervisor === '1';
  var STORE_KEY = 'pos-ticket-' + cfg.session;
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  var state = { lines: [], selected: -1, doc: 'INVOICE' };

  // ── helpers ───────────────────────────────────────────────────────────
  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function num(v) { var n = parseFloat(String(v == null ? '' : v).replace(/,/g, '')); return isFinite(n) ? n : 0; }
  function cents(x) { return Math.round((x + (x >= 0 ? 1e-9 : -1e-9)) * 100) / 100; }
  function fmt(n) { return Number(n).toLocaleString('en-KE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function fmtQty(n) { return String(Math.round(n * 10000) / 10000); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function initials(name) {
    var parts = String(name || '?').replace(/[^\p{L}\p{N}\s]/gu, ' ').trim().split(/\s+/);
    return ((parts[0] || '?')[0] + (parts[1] ? parts[1][0] : (parts[0][1] || ''))).toUpperCase();
  }
  function toast(message, kind) {
    var box = $('.pos-flash');
    if (!box) { box = el('div', 'pos-flash'); box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
    var a = el('div', 'alert shadow-pop ' + ({ error: 'alert-danger', success: 'alert-success', warning: 'alert-warning' }[kind] || 'alert-info'));
    a.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    a.appendChild(el('div', 'alert-body', message));
    box.appendChild(a);
    setTimeout(function () { a.remove(); }, 4000);
  }
  function lineMoney(line) {
    var gross = cents(num(line.price) * line.qty);
    var off = line.disc ? cents(gross * line.disc / 100) : 0;
    var net = cents(gross - off);
    var tax = cents(net * num(line.rate) / 100);
    return { gross: gross, off: off, net: net, tax: tax, amount: cents(net + tax) };
  }

  // ── storage (the ticket survives a lock, a reload or a slip of the finger) ──
  function save() {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({
        lines: state.lines, selected: state.selected,
        customer: $('#pos-customer').value, salesperson: $('#pos-salesperson').value,
        reference: $('#pos-reference').value
      }));
    } catch (e) { /* private mode or storage blocked: the ticket just lives in memory */ }
  }
  function restore() {
    var keys = Object.keys(SERVER_TICKET || {});
    if (keys.length) {
      // A post the server refused comes back with its lines; they win.
      keys.forEach(function (pk) {
        var p = byId[pk];
        var pair = SERVER_TICKET[pk];
        if (p) state.lines.push(makeLine(p, num(pair[0]) || 1, num(pair[1])));
      });
      state.selected = state.lines.length - 1;
      return;
    }
    try {
      var raw = sessionStorage.getItem(STORE_KEY);
      if (!raw) return;
      var saved = JSON.parse(raw);
      state.lines = (saved.lines || []).filter(function (l) { return l && l.id && l.qty > 0; });
      state.selected = Math.min(saved.selected == null ? -1 : saved.selected, state.lines.length - 1);
      if (saved.customer != null) $('#pos-customer').value = saved.customer;
      if (saved.salesperson != null) $('#pos-salesperson').value = saved.salesperson;
      if (saved.reference != null) $('#pos-reference').value = saved.reference;
    } catch (e) { /* unreadable storage: start clean */ }
  }
  function forget() { try { sessionStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ } }

  // ── ticket ────────────────────────────────────────────────────────────
  function makeLine(p, qty, disc) {
    return { id: p.id, sku: p.sku, name: p.name, unit: p.unit, price: p.unit_price || p.price,
             rate: p.tax_rate, qty: qty, disc: disc || 0 };
  }
  function addProduct(p, qty) {
    if (!p) return;
    byId[p.id] = byId[p.id] || p;
    qty = qty || 1;
    for (var i = 0; i < state.lines.length; i++) {
      if (state.lines[i].id === p.id) {
        state.lines[i].qty = Math.round((state.lines[i].qty + qty) * 10000) / 10000;
        state.selected = i;
        render();
        return;
      }
    }
    state.lines.push(makeLine(p, qty, 0));
    state.selected = state.lines.length - 1;
    render();
  }
  function selectedLine() { return state.lines[state.selected] || null; }

  function render() {
    var body = $('#pos-lines');
    body.textContent = '';
    var total = 0, vat = 0, disc = 0, count = 0;
    state.lines.forEach(function (line, i) {
      var m = lineMoney(line);
      total += m.amount; vat += m.tax; disc += m.off; count += line.qty;
      var tr = el('tr', i === state.selected ? 'is-selected' : '');
      tr.dataset.index = i;
      var code = el('td');
      code.appendChild(el('span', 'pos-line-code', line.sku));
      code.appendChild(el('span', 'pos-line-name', line.name));
      tr.appendChild(code);
      tr.appendChild(el('td', '', line.unit || ''));
      tr.appendChild(el('td', 'num', fmtQty(line.qty)));
      tr.appendChild(el('td', 'num', fmt(num(line.price))));
      tr.appendChild(el('td', 'num', line.disc ? String(line.disc) : '0'));
      tr.appendChild(el('td', 'num font-semibold', fmt(m.amount)));
      if (i === state.selected) tr.setAttribute('aria-selected', 'true');
      body.appendChild(tr);
    });
    $('#pos-empty').hidden = state.lines.length > 0;
    $('#pos-total').textContent = fmt(cents(total));
    $('#pos-vat').textContent = fmt(cents(vat));
    $('#pos-count').textContent = fmtQty(count);
    $('#pos-disc').textContent = fmt(cents(disc));
    $('#pos-disc-wrap').hidden = !(disc > 0);
    var sel = body.querySelector('tr.is-selected');
    if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
    renderTileCounts();
    save();
  }
  function ticketTotals() {
    var total = 0, vat = 0;
    state.lines.forEach(function (l) { var m = lineMoney(l); total += m.amount; vat += m.tax; });
    return { total: cents(total), vat: cents(vat) };
  }
  function move(to) {
    if (!state.lines.length) return;
    var n = state.lines.length;
    var i = { first: 0, last: n - 1, prev: Math.max(0, state.selected - 1),
              next: Math.min(n - 1, state.selected + 1) }[to];
    state.selected = i == null ? state.selected : i;
    render();
  }
  function bump(by) {
    var line = selectedLine();
    if (!line) return;
    var next = Math.round((line.qty + by) * 10000) / 10000;
    if (next <= 0) { removeSelected(); return; }
    line.qty = next;
    render();
  }
  function removeSelected() {
    if (state.selected < 0) return;
    state.lines.splice(state.selected, 1);
    state.selected = Math.min(state.selected, state.lines.length - 1);
    render();
  }
  $('#pos-lines').addEventListener('click', function (e) {
    var tr = e.target.closest('tr');
    if (!tr) return;
    state.selected = +tr.dataset.index;
    render();
  });
  $('#pos-lines').addEventListener('dblclick', function () { openKeypad('qty'); });
  $$('[data-nav]').forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.nav === 'remove') removeSelected(); else move(b.dataset.nav);
    });
  });

  // ── picture grid: one screen per top-level category ───────────────────
  var screens = $$('.pos-screen-tab');
  var screenIndex = 0;
  function renderTiles() {
    var key = screens[screenIndex] ? screens[screenIndex].dataset.screen : '';
    var box = $('#pos-tiles');
    box.textContent = '';
    var shown = PRODUCTS.filter(function (p) { return !key || p.screen === key; });
    shown.forEach(function (p) {
      var b = el('button', 'pos-tile');
      b.type = 'button';
      b.dataset.id = p.id;
      b.setAttribute('aria-label', 'Add one ' + p.name);
      var pic = el('span', 'pos-tile-pic');
      if (p.image) {
        var img = el('img'); img.src = p.image; img.alt = ''; img.loading = 'lazy';
        pic.appendChild(img);
      } else {
        pic.appendChild(el('span', 'pos-tile-initials', initials(p.name)));
      }
      b.appendChild(pic);
      var tb = el('span', 'pos-tile-body');
      tb.appendChild(el('span', 'pos-tile-name', p.name));
      var row = el('span', 'flex items-center');
      row.appendChild(el('span', 'pos-tile-price', fmt(num(p.price))));
      tb.appendChild(row);
      b.appendChild(tb);
      b.addEventListener('click', function () { addProduct(p, 1); });
      box.appendChild(b);
    });
    if (!shown.length) box.appendChild(el('p', 'pos-empty col-span-full', 'No products on this screen.'));
    screens.forEach(function (s, i) {
      s.classList.toggle('is-active', i === screenIndex);
      s.setAttribute('aria-selected', i === screenIndex ? 'true' : 'false');
    });
    $('#pos-screen-label').textContent = 'Screen ' + (screenIndex + 1) + ' of ' + screens.length;
    renderTileCounts();
  }
  function renderTileCounts() {
    var counts = {};
    state.lines.forEach(function (l) { counts[l.id] = l.qty; });
    $$('.pos-tile').forEach(function (t) {
      var old = t.querySelector('.pos-tile-count');
      if (old) old.remove();
      if (counts[t.dataset.id]) {
        t.querySelector('.pos-tile-body .flex').appendChild(el('span', 'pos-tile-count', fmtQty(counts[t.dataset.id])));
      }
    });
  }
  screens.forEach(function (s, i) { s.addEventListener('click', function () { screenIndex = i; renderTiles(); }); });
  $('#pos-screen-prev').addEventListener('click', function () { screenIndex = (screenIndex - 1 + screens.length) % screens.length; renderTiles(); });
  $('#pos-screen-next').addEventListener('click', function () { screenIndex = (screenIndex + 1) % screens.length; renderTiles(); });

  // ── server lookups ────────────────────────────────────────────────────
  var warehouse = $('#pos-warehouse').value;
  function lookup(params) {
    var qs = new URLSearchParams(params);
    if (warehouse && !qs.has('warehouse')) qs.set('warehouse', warehouse);
    return fetch(cfg.lookupUrl + '?' + qs.toString(), { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
  }

  // ── code box: scan a barcode or type a SKU, Enter adds it ─────────────
  var code = $('#pos-code');
  $('#pos-code-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var text = code.value.trim();
    if (!text) { openItems(''); return; }
    var qty = 1, m = text.match(/^(\d+(?:\.\d+)?)\*(.+)$/);   // "3*SUG-1" rings three
    if (m) { qty = num(m[1]); text = m[2].trim(); }
    var low = text.toLowerCase();
    var hit = PRODUCTS.filter(function (p) { return p.sku.toLowerCase() === low || (p.barcode && p.barcode === text); })[0];
    if (hit) { addProduct(hit, qty); code.value = ''; return; }
    lookup({ q: text, exact: '1' }).then(function (data) {
      if (data.results.length === 1) { addProduct(data.results[0], qty); code.value = ''; }
      else { code.value = ''; openItems(text); }
    }).catch(function () { toast('Could not reach the item list — check the connection.', 'error'); });
  });

  // ── dialogs ───────────────────────────────────────────────────────────
  function open(id) {
    var d = document.getElementById(id);
    if (!d || d.open) return d;
    d.showModal();
    var first = d.querySelector('[autofocus], input:not([type=hidden]), select, button:not([data-close])');
    if (first) setTimeout(function () { first.focus(); if (first.select) first.select(); }, 0);
    return d;
  }
  function close(d) { if (d && d.open) d.close(); }
  function anyOpen() { return $$('dialog').some(function (d) { return d.open; }) || !$('#pos-lock').hidden; }
  $$('dialog [data-close]').forEach(function (b) { b.addEventListener('click', function () { close(b.closest('dialog')); }); });
  $$('[data-open]').forEach(function (b) { b.addEventListener('click', function () { open(b.dataset.open); }); });
  $$('dialog').forEach(function (d) { d.addEventListener('close', function () { if (!anyOpen()) code.focus(); }); });

  var confirmAction = null;
  function confirmBox(title, text, okLabel, action) {
    var d = $('#dlg-confirm');
    $('[data-title]', d).textContent = title;
    $('[data-text]', d).textContent = text;
    $('[data-ok]', d).textContent = okLabel || 'Yes';
    confirmAction = action;
    open('dlg-confirm');
    setTimeout(function () { $('[data-ok]', d).focus(); }, 0);
  }
  $('#dlg-confirm form').addEventListener('submit', function () {
    var act = confirmAction; confirmAction = null;
    if (act) setTimeout(act, 0);
  });

  // keypads (F1 quantity, F4 discount)
  function wireKeypad(form, input) {
    $$('[data-key]', form).forEach(function (k) {
      k.addEventListener('click', function () {
        var key = k.dataset.key;
        if (key === 'back') input.value = input.value.slice(0, -1);
        else if (key === '.' && input.value.indexOf('.') >= 0) return;
        else input.value += key;
        input.dispatchEvent(new Event('input'));
        input.focus();
      });
    });
  }
  var keypadKind = null;
  function openKeypad(kind) {
    var line = selectedLine();
    if (!line) { toast('Pick a line on the ticket first.', 'warning'); return; }
    keypadKind = kind;
    var d = document.getElementById(kind === 'qty' ? 'dlg-qty' : 'dlg-discount');
    $('[data-line-label]', d).textContent = line.sku + ' · ' + line.name;
    $('[data-readout]', d).value = kind === 'qty' ? fmtQty(line.qty) : String(line.disc || '');
    $('[data-error]', d).textContent = '';
    open(d.id);
  }
  $$('form[data-keypad]').forEach(function (form) {
    var input = $('[data-readout]', form);
    wireKeypad(form, input);
    form.addEventListener('submit', function (e) {
      var line = selectedLine();
      var text = input.value.trim();
      var err = $('[data-error]', form);
      if (!line) return;
      if (form.dataset.keypad === 'qty') {
        if (!/^\d+(\.\d{1,4})?$/.test(text) || num(text) <= 0) {
          e.preventDefault();
          err.textContent = 'A quantity above nothing, up to four decimals.';
          return;
        }
        line.qty = num(text);
      } else {
        if (text === '') text = '0';
        if (!/^\d+(\.\d{1,2})?$/.test(text) || num(text) > 100) {
          e.preventDefault();
          err.textContent = 'A percentage from 0 to 100, up to two decimals.';
          return;
        }
        if (num(text) >= 100) {
          e.preventDefault();
          err.textContent = 'A 100% discount gives the goods away — the till sells, it does not give.';
          return;
        }
        line.disc = num(text);
      }
      render();
    });
  });

  // ── item modal (Select Item…) ─────────────────────────────────────────
  var items = { q: '', category: '', page: 1, pages: 1, rows: [], picked: -1 };
  function categoryPath(id) {
    var names = [], seen = {};
    while (id && catById[id] && !seen[id]) { seen[id] = 1; names.unshift(catById[id].name); id = catById[id].parent; }
    return names.join('>');
  }
  function loadItems() {
    var tbody = $('#items-rows');
    lookup({ q: items.q, category: items.category, page: items.page }).then(function (data) {
      items.rows = data.results; items.page = data.page; items.pages = data.num_pages; items.picked = data.results.length ? 0 : -1;
      if (data.warehouse) $('#items-loc-name').textContent = data.warehouse.name;
      $('#items-page').textContent = items.page;
      $('#items-pages').textContent = items.pages;
      tbody.textContent = '';
      if (!data.results.length) {
        var tr = el('tr'); var td = el('td', 'text-center text-ink-muted', 'Nothing matches.'); td.colSpan = 6; tr.appendChild(td); tbody.appendChild(tr);
        return;
      }
      data.results.forEach(function (p, i) {
        var tr = el('tr', i === items.picked ? 'is-selected' : '');
        tr.dataset.index = i;
        tr.appendChild(el('td', 'text-xs text-ink-muted', p.type === 'SERVICE' ? 'Service' : 'Stock'));
        tr.appendChild(el('td', 'mono', p.sku));
        tr.appendChild(el('td', '', p.name));
        tr.appendChild(el('td', 'mono', p.barcode || ''));
        tr.appendChild(el('td', 'num', fmt(num(p.price))));
        tr.appendChild(el('td', 'num', p.on_hand == null ? '—' : fmtQty(num(p.on_hand))));
        tbody.appendChild(tr);
      });
    }).catch(function () {
      tbody.textContent = '';
      var tr = el('tr'); var td = el('td', 'text-center text-danger', 'Could not load the item list.'); td.colSpan = 6; tr.appendChild(td); tbody.appendChild(tr);
    });
  }
  function pickRow(i) {
    items.picked = i;
    $$('#items-rows tr').forEach(function (tr) { tr.classList.toggle('is-selected', +tr.dataset.index === i); });
  }
  function openItems(q) {
    items.q = q || ''; items.page = 1;
    $('#items-q').value = items.q;
    open('dlg-items');
    loadItems();
  }
  function setCategory(id) {
    items.category = id || '';
    $('#items-filter-path').textContent = id ? categoryPath(id) : 'All categories';
    $('#items-filter-clear').hidden = !id;
    items.page = 1;
    loadItems();
  }
  var qTimer = null;
  $('#items-q').addEventListener('input', function () {
    clearTimeout(qTimer);
    qTimer = setTimeout(function () { items.q = $('#items-q').value.trim(); items.page = 1; loadItems(); }, 250);
  });
  $('#items-q').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); addPicked(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); pickRow(Math.min(items.rows.length - 1, items.picked + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); pickRow(Math.max(0, items.picked - 1)); }
  });
  $('#items-rows').addEventListener('click', function (e) { var tr = e.target.closest('tr'); if (tr && tr.dataset.index) pickRow(+tr.dataset.index); });
  $('#items-rows').addEventListener('dblclick', function (e) { var tr = e.target.closest('tr'); if (tr && tr.dataset.index) { pickRow(+tr.dataset.index); addPicked(); } });
  function addPicked() {
    var p = items.rows[items.picked];
    if (!p) return;
    addProduct(p, 1);
    close($('#dlg-items'));
  }
  $('#items-ok').addEventListener('click', addPicked);
  $$('#dlg-items [data-page]').forEach(function (b) {
    b.addEventListener('click', function () {
      var to = { first: 1, prev: Math.max(1, items.page - 1), next: Math.min(items.pages, items.page + 1), last: items.pages }[b.dataset.page];
      if (to !== items.page) { items.page = to; loadItems(); }
    });
  });
  $('#items-filter-clear').addEventListener('click', function () { setCategory(''); });

  // Category cascade: up to three levels
  var cascade = { path: [] };
  function children(parent) { return CATEGORIES.filter(function (c) { return c.parent === parent; }); }
  function renderCascade() {
    for (var level = 0; level < 3; level++) {
      var ul = $('#cat-cascade ul[data-level="' + level + '"]');
      ul.textContent = '';
      var parent = level === 0 ? '' : cascade.path[level - 1];
      if (level > 0 && !parent) continue;
      children(parent).forEach(function (c) {
        var li = el('li');
        var b = el('button', cascade.path[level] === c.id ? 'is-active' : '');
        b.type = 'button';
        b.appendChild(el('span', '', c.name));
        if (children(c.id).length && level < 2) b.appendChild(el('span', 'text-ink-faint', '›'));
        (function (lv, id) {
          b.addEventListener('click', function () { cascade.path = cascade.path.slice(0, lv); cascade.path[lv] = id; renderCascade(); });
        })(level, c.id);
        li.appendChild(b);
        ul.appendChild(li);
      });
      if (!ul.children.length) ul.appendChild(el('li', 'px-3 py-3 text-sm text-ink-muted', level === 0 ? 'No categories yet.' : 'Nothing under it.'));
    }
    var last = cascade.path[cascade.path.length - 1];
    $('#cat-path').textContent = last ? categoryPath(last) : 'All categories';
  }
  $('#items-cat-btn').addEventListener('click', function () {
    cascade.path = [];
    var id = items.category, chain = [];
    while (id && catById[id]) { chain.unshift(id); id = catById[id].parent; }
    cascade.path = chain.slice(0, 3);
    renderCascade();
    open('dlg-category');
  });
  $('#cat-ok').addEventListener('click', function () { setCategory(cascade.path[cascade.path.length - 1] || ''); close($('#dlg-category')); });
  $('#cat-all').addEventListener('click', function () { setCategory(''); close($('#dlg-category')); });

  // Locations: the till's own shelves
  $('#items-loc-btn').addEventListener('click', function () { open('dlg-locations'); });
  $$('#loc-list [data-warehouse]').forEach(function (b) {
    b.addEventListener('click', function () {
      warehouse = b.dataset.warehouse;
      $('#pos-warehouse').value = warehouse;
      $$('#loc-list [data-warehouse]').forEach(function (x) { x.classList.toggle('is-active', x === b); });
      $('#pos-shelf-name').textContent = b.dataset.name;
      $('#items-loc-name').textContent = b.dataset.name;
      close($('#dlg-locations'));
      if ($('#dlg-items').open) loadItems();
    });
  });

  // ── F10 price / stock check (read only) ───────────────────────────────
  $('#price-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var q = $('#price-q').value.trim();
    var box = $('#price-results');
    if (!q) return;
    box.textContent = '';
    box.appendChild(el('p', 'text-sm text-ink-muted', 'Looking…'));
    lookup({ q: q, stock: 'all' }).then(function (data) {
      box.textContent = '';
      if (!data.results.length) { box.appendChild(el('p', 'text-sm text-ink-muted', 'Nothing called “' + q + '” is on the list.')); return; }
      data.results.slice(0, 8).forEach(function (p) {
        var card = el('div', 'card p-4');
        var head = el('div', 'flex flex-wrap items-baseline gap-x-3');
        head.appendChild(el('strong', 'text-base text-ink', p.name));
        head.appendChild(el('span', 'mono text-ink-muted', p.sku + (p.barcode ? ' · ' + p.barcode : '')));
        head.appendChild(el('span', 'ml-auto text-xl font-extrabold text-ink', cfg.currency + ' ' + fmt(num(p.price))));
        card.appendChild(head);
        card.appendChild(el('p', 'mt-1 text-sm text-ink-muted',
          (num(p.tax_rate) ? p.tax_label + (p.tax_label.indexOf('%') < 0 ? ' (' + num(p.tax_rate) + '%)' : '') + ' · ' + fmt(cents(num(p.price) * (1 + num(p.tax_rate) / 100))) + ' with VAT' : 'No VAT') + ' · per ' + (p.unit || 'unit')));
        if (p.stock === null || p.stock === undefined) {
          card.appendChild(el('p', 'mt-2 text-sm text-ink-muted', 'Not a stocked item.'));
        } else if (!p.stock.length) {
          card.appendChild(el('p', 'mt-2 text-sm text-danger', 'None on hand anywhere.'));
        } else {
          var dl = el('dl', 'dl-rows mt-2');
          p.stock.forEach(function (s) {
            var row = el('div');
            row.appendChild(el('dt', '', s.warehouse));
            row.appendChild(el('dd', 'num' + (num(s.qty) <= 0 ? ' text-danger' : ''), fmtQty(num(s.qty))));
            dl.appendChild(row);
          });
          card.appendChild(dl);
        }
        box.appendChild(card);
      });
      if (data.count > 8) box.appendChild(el('p', 'text-xs text-ink-muted', data.count + ' match — type more of the name to narrow it.'));
    }).catch(function () { box.textContent = ''; box.appendChild(el('p', 'text-sm text-danger', 'Could not reach the item list.')); });
  });

  // ── F2 payout / pay-in ────────────────────────────────────────────────
  function fillAccounts(direction) {
    var sel = $('#payout-account');
    sel.textContent = '';
    (ACCOUNTS[direction] || []).forEach(function (a) {
      var o = el('option', '', a.label); o.value = a.id;
      if (a.id === ACCOUNT_DEFAULTS[direction]) o.selected = true;
      sel.appendChild(o);
    });
    $('#payout-direction').value = direction;
    $('#payout-help').textContent = direction === 'OUT'
      ? 'Dr this account, Cr the till (1100). The drawer gives the notes out.'
      : 'Dr the till (1100), Cr this account. The notes go into the drawer.';
    $$('#dlg-payout [data-direction]').forEach(function (b) {
      var on = b.dataset.direction === direction;
      b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  $$('#dlg-payout [data-direction]').forEach(function (b) { b.addEventListener('click', function () { fillAccounts(b.dataset.direction); }); });
  $('#payout-form').addEventListener('submit', function (e) {
    var amt = $('#payout-amount').value.trim();
    if (!(num(amt) > 0)) { e.preventDefault(); $('#payout-amount').focus(); return; }
    $$('button[type=submit]', e.target).forEach(function (b) { b.disabled = true; });
  });

  // ── Load BOM ──────────────────────────────────────────────────────────
  var bomPicked = BOMS.length ? 0 : -1;
  function renderBoms() {
    var box = $('#bom-list');
    box.textContent = '';
    BOMS.forEach(function (b, i) {
      var btn = el('button', 'pos-choice' + (i === bomPicked ? ' is-active' : ''));
      btn.type = 'button';
      var left = el('span');
      left.appendChild(el('span', 'block', b.name + ' (v' + b.version + ')'));
      left.appendChild(el('span', 'block text-xs font-medium opacity-70', b.lines.map(function (l) { return fmtQty(num(l.qty)) + ' × ' + l.name; }).join(', ')));
      btn.appendChild(left);
      btn.addEventListener('click', function () { bomPicked = i; renderBoms(); });
      box.appendChild(btn);
    });
  }
  $('#bom-ok').addEventListener('click', function () {
    var bom = BOMS[bomPicked];
    var kits = num($('#bom-kits').value);
    var err = $('#dlg-bom [data-error]');
    if (!bom) return;
    if (!(kits > 0)) { err.textContent = 'How many kits — more than nothing.'; return; }
    var missing = [];
    var todo = bom.lines.map(function (l) { return { line: l, qty: Math.round(num(l.qty) * kits * 10000) / 10000 }; });
    var pending = todo.filter(function (t) { return !byId[t.line.id]; });
    var finish = function () {
      todo.forEach(function (t) { if (byId[t.line.id]) addProduct(byId[t.line.id], t.qty); else missing.push(t.line.name); });
      close($('#dlg-bom'));
      if (missing.length) toast('Not sold at the till (inactive or not found): ' + missing.join(', '), 'warning');
    };
    if (!pending.length) { finish(); return; }
    Promise.all(pending.map(function (t) {
      return lookup({ q: t.line.sku, exact: '1' }).then(function (d) {
        d.results.forEach(function (p) { if (p.id === t.line.id) byId[p.id] = p; });
      });
    })).then(finish).catch(finish);
  });

  // ── payment panel (Amount Tendered) ───────────────────────────────────
  var method = $('#pay-method').value || 'CASH';
  function setMethod(m) {
    method = m;
    $('#pay-method').value = m;
    $$('#dlg-pay [data-method]').forEach(function (b) {
      var on = b.dataset.method === m;
      b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var cash = m === 'CASH';
    $('#pay-tendered').disabled = !cash;
    $$('#pay-keypad button, #pay-quick button').forEach(function (b) { b.disabled = !cash; });
    if (!cash) $('#pay-tendered').value = '';
    payUpdate();
  }
  function payUpdate() {
    var t = ticketTotals();
    $('#pay-total').textContent = cfg.currency + ' ' + fmt(t.total);
    $('#pay-vat').textContent = fmt(t.vat);
    var text = $('#pay-tendered').value.trim();
    var given = text === '' ? t.total : num(text);
    var box = $('#pay-change-box');
    var short = method === 'CASH' && given < t.total;
    box.classList.toggle('is-short', short);
    $('#pay-change-label').textContent = method !== 'CASH' ? 'No change — paid exactly' : (short ? 'Short by' : 'Change due');
    $('#pay-change').textContent = fmt(method !== 'CASH' ? 0 : Math.abs(cents(given - t.total)));
    var quick = $('#pay-quick');
    quick.textContent = '';
    var seen = {};
    [t.total, Math.ceil(t.total / 50) * 50, Math.ceil(t.total / 100) * 100, Math.ceil(t.total / 500) * 500, Math.ceil(t.total / 1000) * 1000].forEach(function (v, i) {
      if (v <= 0 || seen[v]) return; seen[v] = 1;
      var b = el('button', 'btn btn-secondary btn-sm', i === 0 ? 'Exact' : fmt(v));
      b.type = 'button'; b.disabled = method !== 'CASH';
      b.addEventListener('click', function () { $('#pay-tendered').value = i === 0 ? '' : String(v); payUpdate(); });
      quick.appendChild(b);
    });
  }
  $$('#dlg-pay [data-method]').forEach(function (b) { b.addEventListener('click', function () { setMethod(b.dataset.method); }); });
  wireKeypad($('#dlg-pay'), $('#pay-tendered'));
  $('#pay-keypad').addEventListener('click', payUpdate);
  $('#pay-tendered').addEventListener('input', payUpdate);
  function openPay() {
    if (!state.lines.length) { toast('Nothing on the ticket yet.', 'warning'); return; }
    if (state.doc === 'RETURN') { setDoc('INVOICE'); }
    $('#dlg-pay [data-error]').textContent = '';
    setMethod(method);
    open('dlg-pay');
    setTimeout(function () { (method === 'CASH' ? $('#pay-tendered') : $('#pay-complete')).focus(); }, 0);
  }
  var submitting = false;
  $('#pay-form').addEventListener('submit', function (e) {
    e.preventDefault();
    if (submitting) return;
    var t = ticketTotals();
    var text = $('#pay-tendered').value.trim();
    var err = $('#dlg-pay [data-error]');
    if (method === 'CASH' && text !== '' && (!/^\d+(\.\d{1,2})?$/.test(text.replace(/,/g, '')) || num(text) < t.total)) {
      err.textContent = 'Cash tendered has to cover the bill of ' + fmt(t.total) + ' (leave it blank for the exact amount).';
      return;
    }
    // Write the ticket into the existing POST contract.
    var holder = $('#pos-line-inputs');
    holder.textContent = '';
    state.lines.forEach(function (l) {
      var q = el('input'); q.type = 'hidden'; q.name = 'qty_' + l.id; q.value = fmtQty(l.qty); holder.appendChild(q);
      if (l.disc) { var d = el('input'); d.type = 'hidden'; d.name = 'disc_' + l.id; d.value = String(l.disc); holder.appendChild(d); }
    });
    submitting = true;
    $('#pay-complete').disabled = true;
    forget();   // a refused post comes back with its lines from the server
    $('#till-form').submit();
  });

  // ── document type (F12) ───────────────────────────────────────────────
  function setDoc(doc) {
    state.doc = doc;
    var label = doc === 'RETURN' ? 'Return' : 'Invoice';
    $('#pos-doc-title').textContent = label;
    $('#pos-doc-sub').textContent = '(' + label + ')';
    $$('#dlg-doctype [data-doctype]').forEach(function (b) { b.classList.toggle('is-active', b.dataset.doctype === doc); });
  }
  $$('#dlg-doctype [data-doctype]').forEach(function (b) {
    b.addEventListener('click', function () {
      setDoc(b.dataset.doctype);
      close($('#dlg-doctype'));
      if (b.dataset.doctype === 'RETURN') openReturn('Process return');
    });
  });
  function openReturn(title) {
    var d = $('#dlg-return');
    $('[data-title]', d).textContent = title;
    open('dlg-return');
  }
  $('#dlg-return').addEventListener('close', function () { if (state.doc === 'RETURN') setDoc('INVOICE'); });

  // ── lock (F7) ─────────────────────────────────────────────────────────
  function post(url, data) {
    var body = new URLSearchParams(data || {});
    return fetch(url, { method: 'POST', body: body, credentials: 'same-origin',
      headers: { 'X-CSRFToken': CSRF, 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }); });
  }
  function showLock() {
    $$('dialog').forEach(close);
    save();
    $('#pos-lock').hidden = false;
    $('#lock-error').textContent = '';
    $('#lock-password').value = '';
    setTimeout(function () { $('#lock-password').focus(); }, 0);
  }
  function lockStation() {
    post(cfg.lockUrl).then(function (r) {
      if (r.ok) showLock(); else toast(r.error || 'Could not lock the till.', 'error');
    }).catch(function () { showLock(); });
  }
  $('#lock-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var pw = $('#lock-password').value;
    if (!pw) return;
    post(cfg.unlockUrl, { password: pw }).then(function (r) {
      if (r.ok) { $('#pos-lock').hidden = true; $('#lock-password').value = ''; code.focus(); }
      else { $('#lock-error').textContent = r.error || 'Could not unlock.'; $('#lock-password').select(); }
    }).catch(function () { $('#lock-error').textContent = 'Could not reach the server.'; });
  });

  // ── function keys ─────────────────────────────────────────────────────
  function disabledReason(btn) {
    if (!btn) return '';
    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return btn.title || 'Not available.';
    return '';
  }
  var actions = {
    qty: function () { openKeypad('qty'); },
    discount: function () {
      if (!SUPERVISOR) { toast('Only a supervisor can give a discount.', 'warning'); return; }
      openKeypad('discount');
    },
    void: function () {
      if (!state.lines.length) return;
      confirmBox('Void this sale?', 'Every line on the ticket is cleared. Nothing has been rung or taken.', 'Void sale', function () {
        state.lines = []; state.selected = -1; render();
      });
    },
    price: function () { $('#price-results').textContent = ''; $('#price-q').value = ''; open('dlg-price'); },
    items: function () { openItems(code.value.trim()); },
    lock: lockStation,
    'return': function () { openReturn('Process return'); },
    refund: function () { openReturn('Process refund'); },
    cashout: function () {
      $('#dlg-cashout [data-ticket-warning]').hidden = !state.lines.length;
      open('dlg-cashout');
    },
    finish: function () {
      confirmBox('Finish this session?', 'The till closes without a count. Record the count on the cash-up sheet afterwards to prove the drawer.' +
        (state.lines.length ? ' The lines on the ticket have not been rung and will be lost.' : ''), 'Close the till', function () {
        forget(); $('#finish-form').submit();
      });
    },
    account: function () {
      if (cfg.mayReceive !== '1') { toast('Your role cannot take an account payment.', 'warning'); return; }
      var c = $('#pos-customer').value;
      window.location.href = cfg.paymentUrl + (c ? '?customer=' + encodeURIComponent(c) : '');
    },
    doctype: function () { open('dlg-doctype'); },
    payout: function () { fillAccounts('OUT'); $('#payout-amount').value = ''; $('#payout-reason').value = ''; open('dlg-payout'); },
    tender: openPay,
    bom: function () { renderBoms(); $('#dlg-bom [data-error]').textContent = ''; open('dlg-bom'); }
  };
  function run(fn, btn) {
    var why = disabledReason(btn || $('.pos-fkey[data-fn="' + fn + '"]'));
    if (why && fn !== 'discount' && fn !== 'account') { toast(why, 'warning'); return; }
    if (actions[fn]) actions[fn]();
  }
  $$('[data-fn]').forEach(function (b) { b.addEventListener('click', function () { run(b.dataset.fn, b); }); });

  var FKEYS = { F1: 'qty', F2: 'payout', F3: 'return', F4: 'discount', F5: 'void', F6: 'refund',
                F7: 'lock', F8: 'finish', F9: 'cashout', F10: 'price', F11: 'account', F12: 'doctype' };
  function typing(t) {
    return t && (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' ||
      (t.tagName === 'INPUT' && !/^(button|submit|checkbox|radio)$/.test(t.type)));
  }
  document.addEventListener('keydown', function (e) {
    var locked = !$('#pos-lock').hidden;
    if (/^F\d{1,2}$/.test(e.key)) {
      e.preventDefault();      // no browser help, reload, full screen or devtools at the till
      if (locked || anyOpen()) return;
      if (e.key === 'F12' && e.ctrlKey) { run('tender'); return; }
      if (FKEYS[e.key]) run(FKEYS[e.key]);
      return;
    }
    if (locked || anyOpen()) return;
    var inCode = e.target === code;
    // Some keyboards (and remote desktops) report the number-pad keys by code only.
    if (!e.key || e.key === 'Unidentified') {
      var byCode = { NumpadAdd: '+', NumpadSubtract: '-' }[e.code];
      if (byCode) e = { key: byCode, target: e.target, preventDefault: e.preventDefault.bind(e) };
    }
    if (typing(e.target) && !(inCode && (e.key === 'ArrowUp' || e.key === 'ArrowDown' ||
        ((e.key === '+' || e.key === '-') && code.value === '')))) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    switch (e.key) {
      case 'ArrowUp': e.preventDefault(); move('prev'); return;
      case 'ArrowDown': e.preventDefault(); move('next'); return;
      case '+': e.preventDefault(); bump(1); return;
      case '-': e.preventDefault(); bump(-1); return;
      case 'Delete': e.preventDefault(); removeSelected(); return;
      case 't': case 'T': e.preventDefault(); run('tender'); return;
    }
    // Anything else typed (a scanner's keystrokes) goes to the code box.
    if (e.key.length === 1 && !inCode) { code.focus(); }
  });

  // ── top bar: customer chip, online ────────────────────────────────────
  function showCustomer() {
    var sel = $('#pos-customer');
    var name = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].dataset.name : 'WALK-IN';
    $('#pos-cust-name').textContent = name;
    $('#pos-cust-initials').textContent = sel.value ? initials(name) : 'WI';
  }
  $('#pos-customer').addEventListener('change', function () { showCustomer(); save(); });
  $('#pos-salesperson').addEventListener('change', save);
  $('#pos-reference').addEventListener('input', save);
  function online() {
    var on = navigator.onLine !== false;
    var o = $('#pos-online');
    o.textContent = on ? 'Online' : 'Offline';
    o.classList.toggle('is-offline', !on);
  }
  window.addEventListener('online', online);
  window.addEventListener('offline', online);

  // Server messages step aside after a while so they never hide the code box.
  $$('.pos-flash .alert').forEach(function (a) { setTimeout(function () { a.remove(); }, 6000); });

  // ── start ─────────────────────────────────────────────────────────────
  restore();
  showCustomer();
  online();
  renderTiles();
  render();
  if (cfg.locked === '1') showLock(); else code.focus();
})();
