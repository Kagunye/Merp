# Target look: Caritas Online (cmfb.shiva.co.ke)

Mofisi's own sign-in already follows this product. Every module should now feel
like the same family: warm, calm, banking-grade, with one confident red.

## Tokens (taken from the live site's `:root`)

| Token | Value | Use |
|---|---|---|
| red | `#c6272b` | primary actions, active nav, key accents only |
| red-strong | `#a61f24` | primary hover |
| red-deep | `#7e151a` | pressed / dark-on-red text |
| red-tint | `#fbeded` | selected rows, red chips |
| ink | `#151515` | headings, body text |
| ink-2 | `#3a3633` | secondary text |
| muted | `#5e5955` | labels |
| subtle | `#6b6661` | meta text |
| faint | `#8a847f` | placeholders, disabled |
| bg (canvas) | `#f6f4f2` | page background (warm, not grey) |
| surface | `#ffffff` | cards |
| surface-2 | `#f7f4f2` | table header, inset panels |
| chip / icon-bg | `#f3efec` / `#f6f2ef` | pills, icon tiles |
| border | `#ece6e2` | card and row borders |
| border-strong | `#dcd5d0` | inputs, outline buttons |
| divider | `#f1ece8` | row separators |
| credit / success | `#1e7a4c` / `#1e6b44`, tint `#eaf5ef` | money in, success |
| error | `#b42318` | money out when shown red, errors |
| hero | `#1a1416` (hero-2 `#241c1e`) | dark feature panels |
| on-hero / muted / credit | `#fff` / `#cfc6c2` / `#8fddb3` | text on hero panels |

Radii: sm 10, base 12, md 14, lg 16, xl 20, 2xl 22, pill 999.
Shadows: primary button `0 6px 16px rgba(198,39,43,.22)`; chip `0 1px 2px rgba(21,21,21,.08)`.
Cards have **no** heavy shadow: a 1px `border` line on white is the look.
Motion: standard ease `cubic-bezier(.2,0,0,1)`, enter `cubic-bezier(.05,.7,.1,1)`,
fast 180ms, base 280ms; respect `prefers-reduced-motion`.

## Type
- UI font: **Plus Jakarta Sans** (400/500/600/700/800) from Google Fonts.
- Accent font: **Playfair Display** italic, red, used sparingly for a single
  tagline line (e.g. "Twaimarisha Biashara" on the sign-in and dashboard hero).
- Headings bold 700–800, tight; labels 14px/600; body 15–16px; meta 12–13px.
- Money: tabular numerals, right aligned, bold for totals.

## Components (match the site's CSS)
- `.btn`: inline-flex, height **52px** (sm **44px**), padding 0 20px, radius 12px,
  16px/700, `:active` scale(.98). Primary = red + primary shadow, hover red-strong.
  Outline = white, border-strong, ink, 600. Ghost (on hero) = transparent with
  white 28% border. Done/busy states optional.
- `.input/.select/.textarea`: height 52px, radius 12px, 1px border-strong, white;
  placeholder faint; focus: border ink + `0 0 0 4px rgba(21,21,21,.08)` (no blue).
- `.field` column gap 8px; `.field-label` 14px/600; help 13px muted; error red.
- `.card`: white, 1px border, radius **20px**, padding 20–24px.
- Status chip: pill, 12px/600, padding 7px 12px, with a 7px dot in the status
  colour (success green, warning amber, danger red, neutral faint, info).
- Lists/tables: rows separated by `divider`, row height ~56px, hover surface-2,
  header 12px uppercase muted on surface-2. Money right aligned, credits green,
  debits ink (or red only when the meaning is "overdue/negative").
- Icon tiles: 40–44px squares radius 12, `icon-bg` background, ink icon; red
  icon only for the active/primary item.
- Hero panel (one per key page, not everywhere): `hero` background, radius 22,
  white text, muted labels, credit-green positive figures, optional faint
  angled "sheets" and a soft brand watermark; buttons inside use ghost/primary.
- Segmented pill toggle (like English | Kiswahili): chip background, the active
  segment white with chip shadow.

## Layout
- Page canvas `bg`; content max ~1280px; generous spacing (24px gaps, 32px between sections).
- Sidebar: white surface with border-right (not solid red). Active item: red-tint
  background, red icon and text, radius 12. Section labels small uppercase faint.
  Brand mark at top. Collapsed rail keeps icon tiles.
- Top bar: white, border-bottom, search as a rounded 44px pill field, company
  switcher as a pill, avatar circle.
- Dashboard: a dark hero card first (company name, Playfair tagline, the key
  figure such as cash on hand in large white numerals, quick actions as ghost
  buttons), then KPI cards, charts in cards, then lists.
- Each module home: a light page header + a small hero/summary strip with that
  module's 2–4 key figures, then launcher tiles as cards with icon tiles.
- Detail pages: header with title, status chip and actions; a summary card
  (`dl` two columns); line tables in cards; totals block right aligned.
- Forms: one card, fields in a 2-column grid on desktop, 1 on phone, 52px
  controls, primary action bottom-right (full width on phone).
- Mobile first: works at 375px; sidebar becomes a drawer; tables scroll inside
  their card; primary buttons full width on phone.

## Don't
- No solid-red sidebars or headers, no blue focus rings, no grey `#F5F6F8` canvas,
  no heavy drop shadows on cards, no emoji icons, no inline colour styles.
