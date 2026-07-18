/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/**/*.{html,ts}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: 'var(--primary)',
          dark: 'var(--primary-dark)',
          light: 'var(--primary-light)',
        },
        secondary: {
          DEFAULT: 'var(--secondary)',
          dark: 'var(--secondary-dark)',
          light: 'var(--secondary-light)',
        },
        danger: 'var(--danger)',
        warning: 'var(--warning)',
        info: 'var(--info)',
        success: 'var(--success)',
        surface: {
          1: 'var(--surface-1)',
          2: 'var(--surface-2)',
          3: 'var(--surface-3)',
        },
        'text-strong': 'var(--text-strong)',
        'text-primary': 'var(--text-primary)',
        'text-muted': 'var(--text-muted)',
        'muted-foreground': 'var(--text-muted)',
        // Design system tokens (port). Namespaced so they coexist with the
        // legacy primary/secondary above. Source: src/app/ui/tokens/_tokens.scss.
        'ui-bg': 'var(--ui-bg-base)',
        'ui-card': 'var(--ui-card-bg)',
        'ui-card-border': 'var(--ui-card-border)',
        'ui-accent': 'var(--ui-accent)',
        'ui-success': 'var(--ui-success)',
        'ui-warning': 'var(--ui-warning)',
        'ui-info': 'var(--ui-info)',
        'ui-danger': 'var(--ui-danger)',
      },
      fontFamily: {
        'ui': 'var(--ui-font-ui)',
        'mono-ui': 'var(--ui-font-mono)',
      },
      transitionDuration: {
        'ui-modal': '200ms',
        'ui-reorder': '150ms',
        'ui-fade': '250ms',
      },
      transitionTimingFunction: {
        'ui-modal': 'ease-out',
        'ui-reorder': 'ease-in-out',
      },
      borderRadius: {
        'sm': 'var(--border-radius-sm)',
        'DEFAULT': 'var(--border-radius)',
        'md': 'var(--border-radius-md)',
        'lg': 'var(--border-radius-lg)',
        'full': 'var(--border-radius-full)',
      },
      boxShadow: {
        'sm': 'var(--shadow-sm)',
        'DEFAULT': 'var(--shadow)',
        'md': 'var(--shadow-md)',
        'lg': 'var(--shadow-lg)',
      },
      backdropBlur: {
        'xs': '2px',
        'sm': '4px',
        'DEFAULT': '12px',
        'md': '8px',
        'lg': '16px',
        'xl': '24px',
      },
      keyframes: {
        spin: {
          from: { transform: 'rotate(0deg)' },
          to: { transform: 'rotate(360deg)' },
        },
        'fade-in': {
          from: { opacity: '0', transform: 'translateY(10px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        pulse: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.7' },
        },
        'stage-pulse': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.6' },
        },
      },
      height: {
        30: '7.5rem', /* template release poster h-30 */
      },
      animation: {
        spin: 'spin 1s linear infinite',
        'fade-in': 'fade-in 0.3s ease forwards',
        pulse: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'stage-pulse': 'stage-pulse 1.5s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
