/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: [
    './templates/**/*.html',
    './apps/**/templates/**/*.html',
    './apps/**/*.py',
  ],
  theme: {
    extend: {
      colors: {
        primary: { DEFAULT: '#C8102E', light: '#E8304E', dark: '#9B000E', 50: '#FFF1F2', 100: '#FFE4E6' },
        surface: { DEFAULT: '#FFFFFF', 2: '#F8F9FA', 3: '#F1F3F4' },
        border: { DEFAULT: '#E5E7EB', dark: '#D1D5DB' },
        text: { primary: '#111827', secondary: '#6B7280', muted: '#9CA3AF' },
      },
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      boxShadow: {
        card: '0 1px 3px 0 rgba(0,0,0,0.07), 0 1px 2px -1px rgba(0,0,0,0.04)',
        'card-hover': '0 4px 12px 0 rgba(0,0,0,0.10)',
        sidebar: '2px 0 8px 0 rgba(0,0,0,0.06)',
      },
    },
  },
  plugins: [require('@tailwindcss/forms')],
}
