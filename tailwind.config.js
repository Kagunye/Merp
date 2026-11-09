/** @type {import('tailwindcss').Config} */
// Design tokens live as CSS variables in static/css/tailwind.input.css (one set
// for light, one for [data-theme="dark"]). The colours below point at those
// variables, so `bg-surface`, `text-ink-muted`, `border-line` etc. switch theme
// on their own. Palette: Caritas Online (docs/DESIGN-cmfb.md). See docs/UI.md.
const v = (name) => `rgb(var(--c-${name}) / <alpha-value>)`

module.exports = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: [
    './templates/**/*.html',
    './apps/**/templates/**/*.html',
    './apps/**/*.py',
    './static/pos/**/*.js',
  ],
  // Component variants that are often picked at runtime (`badge-{{ x }}`).
  safelist: [
    { pattern: /^badge-(success|warning|danger|info|neutral|brand)$/ },
    { pattern: /^alert-(success|warning|danger|error|info)$/ },
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: v('brand'), light: v('brand'), dark: v('brand-hover'),
          50: v('brand-50'), 100: v('brand-100'),
        },
        brand: {
          DEFAULT: v('brand'), hover: v('brand-hover'), strong: v('brand-hover'), deep: v('brand-deep'),
          tint: v('brand-50'), 50: v('brand-50'), 100: v('brand-100'), ink: v('brand-ink'),
        },
        canvas: v('canvas'),
        surface: { DEFAULT: v('surface'), 2: v('surface-2'), 3: v('surface-3') },
        chip: v('chip'),
        'icon-bg': v('icon-bg'),
        line: { DEFAULT: v('line'), strong: v('line-strong'), divider: v('divider') },
        divider: v('divider'),
        border: { DEFAULT: v('line'), dark: v('line-strong') },
        ink: { DEFAULT: v('ink'), 2: v('ink-2'), muted: v('ink-muted'), subtle: v('ink-subtle'), faint: v('ink-faint') },
        text: { primary: v('ink'), secondary: v('ink-2'), muted: v('ink-muted') },
        credit: { DEFAULT: v('credit'), bg: v('success-bg') },
        success: { DEFAULT: v('success'), bg: v('success-bg'), line: v('success-line') },
        warning: { DEFAULT: v('warning'), bg: v('warning-bg'), line: v('warning-line') },
        danger: { DEFAULT: v('danger'), bg: v('danger-bg'), line: v('danger-line') },
        info: { DEFAULT: v('info'), bg: v('info-bg'), line: v('info-line') },
        hero: {
          DEFAULT: v('hero'), 2: v('hero-2'),
          ink: v('on-hero'), muted: v('on-hero-muted'), credit: v('on-hero-credit'),
        },
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', 'system-ui', '-apple-system', '"Segoe UI"', 'sans-serif'],
        accent: ['"Playfair Display"', 'Georgia', '"Times New Roman"', 'serif'],
        mono: ['ui-monospace', '"Cascadia Mono"', 'Consolas', '"SF Mono"', 'Menlo', 'monospace'],
      },
      borderRadius: {
        // Brief radii: sm 10, base 12, md 14, lg 16, xl 20, 2xl 22.
        ctl: '12px',
        card: '20px',
        hero: '22px',
      },
      boxShadow: {
        card: 'none',
        'card-hover': '0 6px 18px -8px rgba(21,21,21,.14)',
        chip: '0 1px 2px rgba(21,21,21,.08)',
        primary: '0 6px 16px rgba(198,39,43,.22)',
        pop: '0 16px 40px -8px rgba(26,20,22,.22), 0 4px 10px -4px rgba(26,20,22,.10)',
        sidebar: 'none',
      },
      transitionTimingFunction: {
        standard: 'cubic-bezier(.2,0,0,1)',
        enter: 'cubic-bezier(.05,.7,.1,1)',
      },
    },
  },
  // `class` strategy: the plugin only styles elements that opt in with
  // .form-input/.form-select/...; the app's own field look comes from
  // tailwind.input.css, so a utility class on an input always wins.
  plugins: [require('@tailwindcss/forms')({ strategy: 'class' })],
}
