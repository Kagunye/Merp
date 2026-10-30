# Mofisi UI — design system and page patterns

This is the guide for restyling any page in MERP. Read it once end to end before
touching a module. See every component rendered at **`/dashboard/ui/`**. That
page is the living style guide, and only platform admins can open it.

| What | Where |
|---|---|
| Tokens, base styles, components, app shell, print | `static/css/tailwind.input.css` |
| Tailwind config (colour names → CSS variables) | `tailwind.config.js` |
| Built CSS (commit it) | `static/css/tailwind.css` (`npm run build:css`) |
| Filters and tags (`money`, `qty`, `pct`, `status_badge`, `can_do`) | `apps/core/templatetags/ui_extras.py` |
| Includes | `templates/_includes/` |
| App shell | `templates/base/base.html`, `templates/base/_nav_item.html` |
| Signed-out layout | `templates/accounts/_auth_layout.html` |
| Reference pages | `templates/dashboard/index.html`, `templates/organizations/*`, `templates/accounts/*`, `templates/notifications/list.html` |

> **Build step.** Tailwind only emits a class that a scanned file uses. The
> scanned files are `templates/**/*.html` and `apps/**/*.py`. After every
> template change, run `npm run build:css` and commit `static/css/tailwind.css`.
> If you skip this, a class that works locally can be missing in the build.

---

## 1. Tokens

Colours are CSS variables on `:root`, with a dark set under `[data-theme="dark"]`.
The dark set is used when the user's `theme_preference` is dark, or when it is
"system" and the device is dark. Tailwind exposes the variables under the
names below, so `bg-surface`, `text-ink-muted`, `border-line` and `text-success`
switch theme automatically. Alpha also works: `bg-brand/10`.

| Token | Tailwind name | Use |
|---|---|---|
| `--c-brand` `#C8102E` | `brand`, `primary` | The **one** primary action on a page, the active nav item, focus rings. Nothing else. |
| `--c-brand-hover` | `brand-hover` | Primary button hover |
| `--c-brand-50/100` | `brand-50`, `brand-100` | Active-nav tint, `badge-brand`, `tone-brand` |
| `--c-brand-ink` | `brand-ink` | Brand as text (links); lighter in dark mode |
| `--c-canvas` | `canvas` | Page background |
| `--c-surface` / `-2` / `-3` | `surface`, `surface-2`, `surface-3` | Cards / table header and subtle fill / hover |
| `--c-line` / `-strong` | `line`, `line-strong` | Borders, dividers / input borders |
| `--c-ink` / `-2` / `-muted` | `ink`, `ink-2`, `ink-muted` | Text: primary / secondary / captions |
| `--c-success` (+`-bg`, `-line`) | `success`, `success-bg`, `success-line` | Paid, posted, approved |
| `--c-warning` (…) | `warning…` | Partial, pending, low stock |
| `--c-danger` (…) | `danger…` | Overdue, rejected, negative amounts, destructive actions |
| `--c-info` (…) | `info…` | Sent, open, in progress |

All text pairs meet WCAG AA (4.5:1) in both themes. The ratios are listed at
the top of `tailwind.input.css`. The old names `--primary`, `--primary-dark`,
`text-primary` and `bg-primary-50` still work, so older templates are fine.

**Type.** Inter. Page title `page-title` (text-xl/2xl bold). Card title
`card-title` (text-sm semibold). Body text-sm. Captions text-xs `text-ink-muted`.
Section labels `section-title` (xs, uppercase, tracking).
**Radius.** Cards and tables use `rounded-xl`. Controls and buttons use `rounded-lg`.
**Spacing.** `page-container` sets the page gutter. Sections use `gap-5` or
`space-y-5`. Cards use `p-5`.

---

## 2. Components (copy-paste)

All components live in `@layer components`. Any Tailwind utility on the same
element wins, for example `btn btn-primary w-full`.

### Buttons
```html
<a href="…" class="btn btn-primary">{% include "_includes/icon.html" with name="plus" stroke="2" %}New invoice</a>
<button type="button" class="btn btn-secondary">Export</button>
<button type="button" class="btn btn-ghost">Cancel</button>
<button type="submit" class="btn btn-danger">Delete</button>
<button type="submit" class="btn btn-danger-ghost btn-sm">Remove</button>
<button type="button" class="btn btn-secondary btn-icon" aria-label="More actions">{% include "_includes/icon.html" with name="ellipsis" cls="h-5 w-5" %}</button>
```
Modifiers: `btn-sm`, `btn-lg`, `btn-block` (full width), `btn-icon` (square,
needs `aria-label`). Group buttons with `btn-group`. Every variant includes the
base, so legacy `class="btn-primary"` still renders correctly.
Use **one** `btn-primary` per view. Secondary actions are `btn-secondary` or
`btn-ghost`, and row actions in tables are `btn btn-ghost btn-sm`.

### Cards
```html
<section class="card">
  <div class="card-header">
    <div><h2 class="card-title">Unpaid invoices</h2><p class="card-subtitle">Oldest first</p></div>
    <a href="…" class="card-link">View all →</a>
  </div>
  <div class="card-body">…</div>
  <div class="card-footer"><button class="btn btn-primary">Save</button></div>
</section>
```
When a table sits inside a card, put `overflow-hidden` on the card and
`rounded-none border-0 shadow-none` on the `table-wrap`. The dashboard does this.

### Tables
```html
<div class="table-wrap">                       {# add table-wrap-scroll for a sticky header in a fixed-height box #}
  <table class="table">                         {# + table-zebra, table-compact #}
    <thead><tr><th>Reference</th><th>Customer</th><th>Status</th><th class="num">Amount</th><th><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>
      {% for inv in invoices %}
      <tr>
        <td class="mono"><a href="{% url 'sales:invoice_detail' inv.pk %}" class="row-link">{{ inv.reference }}</a></td>
        <td>{{ inv.customer.name }}</td>
        <td>{% status_badge inv.status inv.get_status_display %}</td>
        <td class="num">{{ inv.total_amount|money }}</td>
        <td class="actions"><a href="…" class="btn btn-ghost btn-sm">Edit</a></td>
      </tr>
      {% empty %}
      <tr><td colspan="5" class="p-0">{% include "_includes/empty_state.html" with title="No invoices yet" compact=True only %}</td></tr>
      {% endfor %}
    </tbody>
    <tfoot><tr><td colspan="3">Total</td><td class="num">{{ total|money }}</td><td></td></tr></tfoot>
  </table>
</div>
```
- `.table` gives an uppercase muted header, row hover and tabular figures.
  The header is `sticky top-0`, and it sticks inside `table-wrap-scroll`.
- `.num` right-aligns a cell, uses tabular numbers and stops wrapping. Use it on
  the `<th>` too.
- `.row-link` is the link style for the main cell. `.actions` right-aligns
  row actions. `.empty-row` is for a short "nothing here" row.
- `.table-wrap` scrolls sideways on phones, so the page never does.

### Badges
```html
{% status_badge obj.status obj.get_status_display %}          {# preferred #}
{% include "_includes/status_badge.html" with status=obj.status label=obj.get_status_display %}
<span class="badge badge-success">Active</span>                 {# manual: success|warning|danger|info|neutral|brand #}
<span class="badge">Plain</span>
```
The status→colour table is `STATUS_VARIANTS` in `ui_extras.py`. It covers
every status code in the models: DRAFT, SENT, PARTIAL, PAID, OVERDUE,
CANCELLED, POSTED, APPROVED, PENDING, REJECTED, OPEN, CLOSED, SUBMITTED,
CONFIRMED and the rest. Add a new code there, not in a template.
`{{ code|status_variant }}` returns the class, and `{{ code|status_label }}`
returns sentence-case text.

### Forms
```html
<form method="post" class="card card-body" novalidate>
  {% csrf_token %}
  <div class="form-grid">                                  {# 1 col, 2 from sm #}
    <div class="form-field">
      <label class="form-label" for="id_name">Name<span class="form-required" aria-hidden="true">*</span></label>
      <input id="id_name" name="name" class="input">
      <p class="form-help">As it appears on invoices.</p>
    </div>
    <div class="form-field">
      <label class="form-label" for="id_type">Type</label>
      <select id="id_type" name="type" class="select">…</select>
    </div>
    <div class="form-field form-field-full">
      <label class="form-label" for="id_notes">Notes</label>
      <textarea id="id_notes" name="notes" class="textarea"></textarea>
    </div>
    <div class="form-field form-field-full">
      <label class="check" for="id_active"><input id="id_active" type="checkbox" name="active" class="checkbox"> Active</label>
    </div>
  </div>
  <div class="form-actions"><a href="…" class="btn btn-ghost">Cancel</a><button type="submit" class="btn btn-primary">Save</button></div>
</form>
```
- Errors: add `is-invalid` (or `aria-invalid="true"`, which Django 5 adds
  itself) and `<p class="form-error">…</p>`. Show non-field errors as
  `<div class="alert alert-danger">`.
- Other pieces: `input-sm`, `.input-group` (an icon followed by `.input`),
  and `form-section` (an uppercase section label).
- **Raw Django widgets** (`{{ field }}`, `{{ form.as_p }}`) inside `<main>`
  already look like `.input`, `.select` and `.textarea`. You don't need widget
  attrs. The base layer styles them at specificity (0,0,1), so any class wins.
- **Bare `<form>` = card (legacy).** A `<form>` in `<main>` is drawn as a
  white card, because about 150 old templates rely on it. Opt out with
  `class="no-card"`, `inline` or `inline-form`. Forms inside `td`, `.card`,
  `.table-wrap`, `.toolbar`, `.filter-bar`, `.page-header`, `.page-actions` or
  `nav` are never carded.
- **Class-less submit button = primary.** `<button type="submit">` with **no
  class** renders as the primary button. Once you give a button any class, only
  that class styles it, so write `btn btn-…`.
- The whole-form shortcut is `{% include "_includes/model_form.html" %}`. See §3.

### Alerts (Django messages)
`base.html` draws every message as an alert. Draw one yourself with:
```html
<div class="alert alert-warning" role="status">
  {% include "_includes/icon.html" with name="warning" %}
  <div class="alert-body"><p class="alert-title">Heads up</p>3 products are below reorder level.</div>
</div>
```
Variants are `alert-success`, `alert-info`, `alert-warning` and `alert-danger`
(or `alert-error`). Message tags map as error→danger, success, warning, and
anything else→info.

### Empty state
```html
{% include "_includes/empty_state.html" with icon="inbox" title="No bills yet" text="Supplier bills you record appear here." action_label="New bill" action_url=new_url %}
```

### Stat (KPI tile)
```html
<div class="stat-grid">   {# 1 → 2 (≥420px) → 3 (md) → 6 (2xl) columns #}
  {% include "_includes/stat.html" with label="Cash on hand" value=total_cash money=True currency=book meta="All bank and cash" icon="cash" tone="info" url=cash_url only %}
</div>
```

### Definition lists
```html
<dl class="dl">                                 {# 1 col, 2 from sm; add dl-3 for 3 from lg #}
  <div><dt>Customer</dt><dd>Acme Ltd</dd></div>
  <div class="dl-full"><dt>Address</dt><dd>…</dd></div>
</dl>
<dl class="dl-rows px-5">                       {# label left, value right: totals, summaries #}
  <div><dt>Subtotal</dt><dd>{{ sub|money }}</dd></div>
</dl>
```

### Toolbar, filter bar, tabs, breadcrumbs
```html
<div class="toolbar"><div class="toolbar-group">…</div><div class="toolbar-group">…</div></div>
{% include "_includes/filter_bar.html" with search_placeholder="Reference or customer" filter_form=filter_form %}
<nav class="tabs" aria-label="Invoice sections">
  <a href="…" class="tab tab-active" aria-current="page">Details</a>
  <a href="…" class="tab">Lines <span class="tab-count">4</span></a>
</nav>
{% include "_includes/breadcrumbs.html" with crumbs=crumbs %}
```

### Smaller pieces
- `avatar` and `avatar-lg` (initials or an `<img>`).
- `list` / `list-row` / `list-row-icon` for feeds and notification lists.
- `menu` / `menu-item` / `menu-label` / `menu-sep` for dropdowns. For the
  toggle, write `<button data-menu="menu-id" aria-expanded="false">` and give
  the panel `id="menu-id" class="menu hidden"`. The script in `base.html`
  handles click-outside and Escape.
- Icon chips: `tone-brand`, `tone-success`, `tone-warning`, `tone-danger`,
  `tone-info` and `tone-neutral`.
- Number colours: `neg` (red) and `pos` (green).
- Text helpers: `muted`, `mono` (references, codes, PINs) and `link`.
- `section-title`.
- `skip-link` and `sr-only` (Tailwind).

---

## 3. Includes and filters

All includes are in `templates/_includes/`. Pass resolved URLs:
`{% url 'x:y' as u %}` first, then `…=u`. **Use `only` when you include
inside a `{% for %}` loop.** A variable such as `url`, `icon` or `label` from
the loop would otherwise leak into the include.

| Include | Parameters |
|---|---|
| `page_header.html` | `title`\*, `subtitle`, `eyebrow`, `back_url`, `back_label`, `crumbs`, `action_label`+`action_url` (primary, shown only when both are set), `action_icon` (`plus` by default, `edit` when the label starts with "Edit", any icon name, or `none`), `secondary_label`+`secondary_url`, `actions_template` (a partial for POST buttons or menus), `hide_print`, `print_label` |
| `filter_bar.html` | `search_name` (default `q`, `"none"` to drop it), `search_value`, `search_placeholder`, `search_label`, `filter_form`, `extra_template`, `submit_label`, `reset_url` |
| `empty_state.html` | `title`\*, `text`, `icon`, `action_label`+`action_url`, `secondary_label`+`secondary_url`, `compact` |
| `stat.html` | `label`\*, `value`, `money`, `qty`, `currency`, `unit`, `meta`, `icon`, `tone`, `url`, `trend` (`up`/`down`), `value_class` |
| `data_table.html` | `table_template`\*, `title`, `subtitle`, `count`, `actions_template`, `empty`, `empty_title`, `empty_text`, `empty_icon`, `empty_action_label`, `empty_action_url`, `scroll`, `paginate` |
| `status_badge.html` | `status`\*, `label` |
| `breadcrumbs.html` | `crumbs` (a list of `{"label","url"}`, where the last item is the current page), `home` (False hides "Home") |
| `icon.html` | `name`\*, `cls` (default `h-4 w-4`), `stroke` (default 1.75), `icon_label` (makes the icon announced). There are 50 names, all listed at `/dashboard/ui/#icons`. |
| `pagination.html` | Takes no parameters. It needs `is_paginated`, `page_obj` and `request`, and keeps the other query params. |
| `model_form.html` | `form`\*, `title`, `subtitle`, `back_url`, `cancel_url`, `submit_label`, `section_label`, `extra_template`. Unchanged from before. |
| `launcher.html` | `launcher_title`, `launcher_subtitle`. The tiles stay inline as `<a … class="launcher-tile">`, and tests count that exact attribute. |
| `print_button.html` | `print_label` |

`{% load ui_extras %}` provides these filters and tags:

| Filter / tag | Example | Output |
|---|---|---|
| `money` | `{{ 126500\|money }}` | `126,500.00` |
| | `{{ x\|money:"KES" }}` | `KES 126,500.00` |
| | `{{ -50\|money }}` | `<span class="neg">-50.00</span>` |
| | `None` / `""` → `—`, junk → shown escaped as is | |
| `qty` | `{{ 1250.5000\|qty }}` / `{{ x\|qty:2 }}` | `1,250.5` / `1,250.50` |
| `pct` | `{{ 16\|pct }}` / `{{ 12.345\|pct:2 }}` | `16%` / `12.35%`. The value must already be a percentage, not a ratio. |
| `status_badge` | `{% status_badge s label %}` | A coloured pill |
| `status_variant` / `status_label` | `{{ s\|status_variant }}` | `badge-success` / `Partially delivered` |
| `can_do` | `{% can_do "sales:invoice_create" "POST" as ok %}` | Whether this role may do it (from `access.py`) |
| `trim_qty`, `with_unit`, `page_url`, `page_window`, `account_*` | Unchanged | |

---

## 4. Page patterns

Every page extends `base/base.html` and fills `{% block content %}`. The shell
supplies the page container, the breadcrumbs from a view's `breadcrumbs`
context, and the messages.

### List page
```django
{% extends "base/base.html" %}{% load ui_extras %}
{% block content %}
{% can_do "sales:invoice_create" "POST" as may_create %}{% if may_create %}{% url 'sales:invoice_create' as new_url %}{% endif %}
{% include "_includes/page_header.html" with title="Invoices" subtitle="Everything you have billed" action_label="New invoice" action_url=new_url %}
{% include "_includes/filter_bar.html" with search_placeholder="Reference or customer" %}   {# keep the GET names the view reads #}
<div class="table-wrap"><table class="table">…</table></div>
{% include "_includes/pagination.html" %}
{% endblock %}
```
References: `organizations/branch_list.html`, `accounts/user_list.html`.

### Detail page
- A page header with `back_url` and an Edit action (gated). Put POST actions
  such as Post, Approve or Void in an `actions_template` partial, each wrapped
  in its own `{% can_do … "POST" %}`.
- A status badge next to the title or in the card header.
- Use `<section class="card"><div class="card-body"><dl class="dl">…` for
  the header fields. Put lines in a `table-wrap` table and totals in `dl-rows`.
- For a side column, use `grid grid-cols-1 lg:grid-cols-3 gap-5`, with
  `lg:col-span-2` for the main column.

References: `accounts/user_detail.html`, `organizations/company_detail.html`.

### Form page
`{% include "_includes/model_form.html" with title=… back_url=back cancel_url=back submit_label="Save branch" %}`.
For a hand-built form, use `card card-body` with `form-grid`, `form-field`
and `form-actions` (see §2). Field `name`s must stay exactly as the view reads them.

### Report page
- A page header with the Print button (shown by default).
- A `filter-bar` for the date range.
- A `stat-grid` for the headline figures.
- One `table-wrap` per section, with `<tfoot>` totals and every amount in
  `|money` inside `.num` cells.
- The print stylesheet drops the shell, filters and buttons, adds the
  letterhead, and repeats table headers on every page.

### Dashboard / home page
See `dashboard/index.html`: stat tiles, chart cards (Chart.js reads colours
from the CSS variables) and card-wrapped tables. Each block is gated on the
same right as the page it links to.

---

## 5. Do / don't

**Do**
- Print money with `|money` in a `.num` cell, always. Use `|qty` for
  quantities and `|pct` for percentages. Never use `floatformat` for amounts.
- Gate every action with `{% can_do "ns:url_name" "POST" as ok %}`. Gate links
  with the GET right. Tests check that a role is never shown a link the role
  table refuses.
- Use one `btn-primary` per view. Make secondary actions `btn-secondary` and
  row actions `btn-ghost btn-sm`.
- Draw statuses with `{% status_badge %}`.
- Use an empty state in every list and table.
- Put `aria-label` on icon-only buttons, `<span class="sr-only">Actions</span>`
  in an empty `<th>`, and `for=`/`id` on every label/control pair. A test
  asserts that every `label[for]` has a matching id.
- Keep context variable names, form field names, POST parameters and URLs
  exactly as they are. Templates change, behaviour doesn't.
- Rebuild the CSS (`npm run build:css`) and commit `static/css/tailwind.css`.

**Don't**
- Don't use inline `style=""` for colour, background or border. The palette is
  in the tokens, and dark mode depends on them. (`style` for a computed width,
  such as a progress bar, is fine. The `account_*` tags emit computed colours by
  design.)
- Don't hard-code hex colours or Tailwind palette colours (`bg-red-50`,
  `text-gray-500`) in new markup. Use the token names (`danger-bg`, `ink-muted`).
- Don't add page-level `<style>` blocks. Add a component to
  `tailwind.input.css` instead.
- Don't put `btn-primary` on destructive actions. Use `btn-danger` and confirm
  with `onsubmit="return confirm(…)"`.
- Don't use emoji as icons in new markup. Use `_includes/icon.html`.
- Don't let the page scroll sideways at 375px. Wide content belongs in
  `table-wrap`.

---

## 6. App shell notes

- **Sidebar** (`base.html` with `base/_nav_item.html`). Groups are Finance,
  Sales, Supply chain, People and Workspace, with Control Panel at the bottom.
  Each item shows only when `nav.<namespace>` is true (`can_view_module`).
  On phones it is a drawer, on tablets a 72px rail that expands as an overlay,
  and on desktop it is expanded with the choice remembered. The active item
  comes from `request.resolver_match.namespace`.
- **Top bar.** It has the search box (to `dashboard:search`), the company and
  branch switcher (`user_companies`, plus `user_branches` from the context
  processor), Print, the notifications dropdown and the user menu. Platform
  admins also get a "UI style guide" link.
- **Accessibility.** There is a skip link to `#main-content`, a visible
  `:focus-visible` ring everywhere, `aria-label` on every icon button,
  `aria-expanded` on the menus, `aria-current` on the active nav item and tab,
  and `role="alert"`/`status` on messages.
- **Print.** The rules are at the end of `tailwind.input.css`: the letterhead
  `.print-header`, `.no-print`, no shell, and black on white.
- **Signed-out pages** extend `accounts/_auth_layout.html`, which provides the
  hero panel, the logo, the messages and the security footer. Its blocks are
  `title`, `hero`, `top_right`, `content` and `scripts`.
