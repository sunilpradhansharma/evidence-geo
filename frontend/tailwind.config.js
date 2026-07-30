/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#0D4F4F",
          light: "#14B8A6",
          dark: "#0A3D3D",
          accent: "#F59E0B",
          surface: "rgb(var(--brand-surface) / <alpha-value>)",
        },
        ink: {
          DEFAULT: "rgb(var(--ink) / <alpha-value>)",
          light: "rgb(var(--ink-light) / <alpha-value>)",
          muted: "rgb(var(--ink-muted) / <alpha-value>)",
        },
        canvas: {
          DEFAULT: "rgb(var(--canvas) / <alpha-value>)",
          card: "rgb(var(--canvas-card) / <alpha-value>)",
        },
        // Magnus-style semantic elevation + hairline tokens (auto-flip in dark).
        surface: {
          0: "rgb(var(--surface-0) / <alpha-value>)",
          1: "rgb(var(--surface-1) / <alpha-value>)",
          2: "rgb(var(--surface-2) / <alpha-value>)",
        },
        line: "rgb(var(--line) / <alpha-value>)",
        status: {
          success: "#0F766E",
          warning: "#EA580C",
          error: "#DC2626",
          info: "#0284C7",
        },
      },
      fontFamily: {
        sans: ['"DM Sans"', "Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ['"Rajdhani"', '"DM Sans"', "ui-sans-serif", "sans-serif"],
        mono: ['"DM Mono"', "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: {
        xs: ["0.875rem", { lineHeight: "1.25rem" }],
        sm: ["0.9375rem", { lineHeight: "1.375rem" }],
        base: ["1.0625rem", { lineHeight: "1.625rem" }],
        lg: ["1.1875rem", { lineHeight: "1.8125rem" }],
        xl: ["1.3125rem", { lineHeight: "1.875rem" }],
        "2xl": ["1.625rem", { lineHeight: "2.125rem" }],
        "3xl": ["2rem", { lineHeight: "2.375rem" }],
      },
    },
  },
  plugins: [],
};
