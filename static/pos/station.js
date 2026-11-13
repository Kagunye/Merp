/* Waiter station, bill and cashier panel: PIN pad, menu tabs, dialogs, live change.
   Plain JS, no dependencies. Every form still posts without it. */
(function () {
  'use strict';
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function num(text) {
    var v = parseFloat(String(text == null ? '' : text).replace(/,/g, ''));
    return isNaN(v) ? null : v;
  }
  function fmt(v) {
    return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function openDialog(dlg) {
    if (!dlg) return;
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    var first = $('select, input:not([type=hidden]):not([type=radio])', dlg);
    if (first) first.focus();
  }

  // ── Dialogs ──
  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-open]');
    if (opener && !opener.disabled) { openDialog(document.getElementById(opener.getAttribute('data-open'))); return; }
    var closer = e.target.closest('[data-close]');
    if (closer) { var d = closer.closest('dialog'); if (d) d.close(); }
  });

  // ── Keypads: data-pad="<input id>", buttons data-key ──
  $$('[data-pad]').forEach(function (pad) {
    var input = document.getElementById(pad.getAttribute('data-pad'));
    if (!input) return;
    pad.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-key]');
      if (!btn) return;
      var k = btn.getAttribute('data-key');
      var max = parseInt(input.getAttribute('maxlength') || '0', 10);
      if (k === 'back') input.value = input.value.slice(0, -1);
      else if (k === 'clear') input.value = '';
      else if (k === '.' && input.value.indexOf('.') !== -1) return;
      else if (!max || input.value.length < max) input.value += k;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  });

  // ── PIN sign-in: tapping a name moves to the PIN ──
  var pinForm = $('#pin-form');
  if (pinForm) {
    var who = $('#pin-who'), pin = $('#pin-input');
    pinForm.addEventListener('change', function (e) {
      if (e.target.name !== 'staff') return;
      if (who) who.textContent = '· ' + (e.target.getAttribute('data-staff-name') || '');
      if (pin) { pin.value = ''; pin.focus(); }
    });
    var checked = $('input[name=staff]:checked', pinForm);
    if (!checked && $$('input[name=staff]', pinForm).length === 1) {
      var only = $('input[name=staff]', pinForm);
      only.checked = true;
      only.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  // ── Menu section tabs ──
  var tabs = $$('[data-tab]');
  function showSection(id) {
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-tab') === id;
      t.classList.toggle('is-active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('[data-tile]').forEach(function (f) {
      f.classList.toggle('hidden', !!id && f.getAttribute('data-tile') !== id);
    });
  }
  if (tabs.length) {
    tabs.forEach(function (t) { t.addEventListener('click', function () { showSection(t.getAttribute('data-tab')); }); });
    var bill = $('#bill');
    var start = bill ? bill.getAttribute('data-section') || '' : '';
    if (start && !tabs.some(function (t) { return t.getAttribute('data-tab') === start; })) start = '';
    showSection(start);
  }

  // ── Quantity and note for the next tap ──
  var qty = $('#add-qty'), note = $('#add-note');
  $$('[data-step]').forEach(function (b) {
    b.addEventListener('click', function () {
      var v = num(qty.value) || 0;
      v = Math.max(1, v + parseInt(b.getAttribute('data-step'), 10));
      qty.value = String(v);
    });
  });
  $$('[data-tile]').forEach(function (form) {
    form.addEventListener('submit', function () {
      var q = num(qty && qty.value);
      $('[data-qty]', form).value = q && q > 0 ? String(q) : '1';
      $('[data-note]', form).value = note ? note.value : '';
    });
  });

  // ── Void one item ──
  var voidDlg = $('#dlg-void-line');
  $$('[data-void-line]').forEach(function (b) {
    b.addEventListener('click', function () {
      if (!voidDlg) return;
      $('[data-line]', voidDlg).value = b.getAttribute('data-void-line');
      $('[data-line-name]', voidDlg).textContent = b.getAttribute('data-name') || '';
      openDialog(voidDlg);
    });
  });

  // ── Pay: show cash only for cash, change worked out live ──
  $$('[data-pay]').forEach(function (form) {
    var total = num(form.getAttribute('data-total')) || 0;
    var cash = $('[data-cash]', form), tendered = $('[data-tendered]', form);
    var box = $('[data-change-box]', form), out = $('[data-change]', form), label = $('[data-change-label]', form);
    var submit = $('[data-pay-submit]', form);
    function method() { var m = $('[data-method]:checked', form); return m ? m.value : 'CASH'; }
    function update() {
      var isCash = method() === 'CASH';
      if (cash) cash.hidden = !isCash;
      if (!isCash) { if (submit) submit.disabled = false; return; }
      var given = num(tendered.value);
      if (given === null) { label.textContent = 'Exact money'; out.textContent = fmt(0); box.classList.remove('is-short'); submit.disabled = false; return; }
      var diff = Math.round((given - total) * 100) / 100;
      box.classList.toggle('is-short', diff < 0);
      label.textContent = diff < 0 ? 'Short by' : 'Change due';
      out.textContent = fmt(Math.abs(diff));
      submit.disabled = diff < 0;
    }
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    form.addEventListener('submit', function () {
      if (method() !== 'CASH' && tendered) tendered.value = '';
      if (submit) { submit.disabled = true; submit.textContent = 'Taking payment…'; }
    });
    update();
  });

  // ── PIN idle: reload when the PIN session lapses, so the pad shows again ──
  var shell = $('[data-idle]');
  if (shell) {
    var minutes = parseFloat(shell.getAttribute('data-idle')) || 0;
    if (minutes > 0) setTimeout(function () { window.location.reload(); }, minutes * 60000 + 1500);
  }
})();
