import type { Config } from 'tailwindcss';

/** Theme-aware token backed by an `R G B` CSS variable (see src/styles/index.css). */
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: ['selector', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        // Surfaces (SPEC §9.1). Swapped wholesale by the light theme.
        ink: {
          950: v('ink-950'),
          900: v('ink-900'),
          850: v('ink-850'),
          800: v('ink-800'),
          700: v('ink-700'),
        },
        // Text
        ice: { 100: v('ice-100') },
        slate: { 400: v('slate-400') },
        muted: v('slate-400'),
        accent: v('accent'),
        ring: v('ring'),
        // Brand (constant across themes; brand moments only)
        brand: {
          cyan: '#0CE7FA',
          sky: '#1DC6EF',
          azure: '#4998D1',
          indigo: '#6456C5',
          violet: '#7A33B3',
        },
        // Status (text-safe per theme)
        success: v('success'),
        warning: v('warning'),
        danger: v('danger'),
        info: v('info'),
      },
      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['"Space Grotesk Variable"', '"Space Grotesk"', '"Inter Variable"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono Variable"', '"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      backgroundImage: {
        'brand-gradient': 'linear-gradient(135deg, #0CE7FA, #4998D1 45%, #7A33B3)',
      },
      boxShadow: {
        raised: '0 1px 0 0 rgb(255 255 255 / 0.03) inset, 0 8px 24px -12px rgb(0 0 0 / 0.5)',
        glow: '0 0 0 1px rgb(29 198 239 / 0.25), 0 8px 32px -8px rgb(73 152 209 / 0.45)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'fade-out': { from: { opacity: '1' }, to: { opacity: '0' } },
        'slide-up': { from: { transform: 'translateY(12px)', opacity: '0' }, to: { transform: 'none', opacity: '1' } },
        'slide-in-right': { from: { transform: 'translateX(24px)', opacity: '0' }, to: { transform: 'none', opacity: '1' } },
        'scale-in': { from: { transform: 'scale(0.97)', opacity: '0' }, to: { transform: 'none', opacity: '1' } },
        shimmer: { from: { backgroundPosition: '-200% 0' }, to: { backgroundPosition: '200% 0' } },
        glow: { '0%, 100%': { opacity: '0.55' }, '50%': { opacity: '0.9' } },
        indeterminate: { from: { transform: 'translateX(-100%)' }, to: { transform: 'translateX(250%)' } },
      },
      animation: {
        'fade-in': 'fade-in 180ms ease-out both',
        'fade-out': 'fade-out 220ms ease-in both',
        'slide-up': 'slide-up 200ms cubic-bezier(.2,.8,.2,1) both',
        'slide-in-right': 'slide-in-right 220ms cubic-bezier(.2,.8,.2,1) both',
        'scale-in': 'scale-in 160ms cubic-bezier(.2,.8,.2,1) both',
        shimmer: 'shimmer 1.6s linear infinite',
        glow: 'glow 2.4s ease-in-out infinite',
        indeterminate: 'indeterminate 1.4s ease-in-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config;
