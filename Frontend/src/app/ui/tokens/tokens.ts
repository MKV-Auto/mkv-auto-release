// TS-side mirror of the design tokens defined in _tokens.scss.
// Keep minimal — only values consumed by component .ts code (timings for
// setTimeout, color literals for inline SVG fills). Templates and SCSS read
// from the CSS variables directly.

export const UiAnimationMs = {
  modal: 200,
  reorder: 150,
  fade: 250,
} as const;

export const UiColors = {
  accent: '#6366f1',
  success: '#10b981',
  warning: '#facc15',
  info: '#60a5fa',
  danger: '#ef4444',
} as const;
