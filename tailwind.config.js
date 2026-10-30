/** @type {import('tailwindcss').Config} */
// Design tokens live as CSS variables in static/css/tailwind.input.css (one set
// for light, one for [data-theme="dark"]). The colours below point at those
// variables, so `bg-surface`, `text-ink-muted`, `border-line` etc. switch theme
// on their own. See docs/UI.md.
const v = (name) => `rgb(var(--c-${name}) / <alpha-value>)`

module.exports = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: [
    './templates/**/*.html',
    './apps/**/templates/**/*.html',
    './apps/**/*.py',
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
          DEFAULT: v('brand'), light: '#E8304E', dark: v('brand-hover'),
          50: v('brand-50'), 100: v('brand-100'),
        },
        brand: { DEFAULT: v('brand'), hover: v('brand-hover'), 50: v('brand-50'), 100: v('brand-100'), ink: v('brand-ink') },
        canvas: v('canvas'),
        surface: { DEFAULT: v('surface'), 2: v('surface-2'), 3: v('surface-3') },
        line: { DEFAULT: v('line'), strong: v('line-strong') },
        border: { DEFAULT: v('line'), dark: v('line-strong') },
        ink: { DEFAULT: v('ink'), 2: v('ink-2'), muted: v('ink-muted') },
        text: { primary: v('ink'), secondary: v('ink-2'), muted: v('ink-muted') },
        success: { DEFAULT: v('success'), bg: v('success-bg'), line: v('success-line') },
        warning: { DEFAULT: v('warning'), bg: v('warning-bg'), line: v('warning-line') },
        danger: { DEFAULT: v('danger'), bg: v('danger-bg'), line: v('danger-line') },
        info: { DEFAULT: v('info'), bg: v('info-bg'), line: v('info-line') },
      },
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      boxShadow: {
        card: '0 1px 2px 0 rgba(16,24,40,0.05)',
        'card-hover': '0 4px 12px 0 rgba(16,24,40,0.08)',
        pop: '0 12px 32px -4px rgba(16,24,40,0.16), 0 4px 8px -2px rgba(16,24,40,0.06)',
        sidebar: '1px 0 0 0 rgba(16,24,40,0.04)',
      },
    },
  },
  // `class` strategy: the plugin only styles elements that opt in with
  // .form-input/.form-select/...; the app's own field look comes from
  // tailwind.input.css, so a utility class on an input always wins.
  plugins: [require('@tailwindcss/forms')({ strategy: 'class' })],
}
