/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        /*
          Theme-driven palette.

          Every entry resolves through the theme contract in `src/index.css`
          (`rgb(var(--…) / <alpha-value>)`, so the `/70` opacity modifiers keep
          working).  The steps below are the ones the components actually use: a
          theme replaces the *values*, the class names never change.

          This block replaced an earlier set of Material-3 names that nothing
          referenced (and whose `surface` key silently shadowed the role of the
          same name).  The three names the shell still uses are kept at the end,
          pointed at the same roles, so nothing can paint a colour the theme
          cannot reach.
        */
        gray: {
          50: "rgb(var(--gray-50) / <alpha-value>)",
          100: "rgb(var(--gray-100) / <alpha-value>)",
          200: "rgb(var(--gray-200) / <alpha-value>)",
          300: "rgb(var(--gray-300) / <alpha-value>)",
          400: "rgb(var(--gray-400) / <alpha-value>)",
          500: "rgb(var(--gray-500) / <alpha-value>)",
          600: "rgb(var(--gray-600) / <alpha-value>)",
          700: "rgb(var(--gray-700) / <alpha-value>)",
          800: "rgb(var(--gray-800) / <alpha-value>)",
          900: "rgb(var(--gray-900) / <alpha-value>)",
        },
        blue: {
          50: "rgb(var(--blue-50) / <alpha-value>)",
          100: "rgb(var(--blue-100) / <alpha-value>)",
          400: "rgb(var(--blue-400) / <alpha-value>)",
          500: "rgb(var(--blue-500) / <alpha-value>)",
          600: "rgb(var(--blue-600) / <alpha-value>)",
          700: "rgb(var(--blue-700) / <alpha-value>)",
        },
        red: {
          50: "rgb(var(--red-50) / <alpha-value>)",
          100: "rgb(var(--red-100) / <alpha-value>)",
          200: "rgb(var(--red-200) / <alpha-value>)",
          300: "rgb(var(--red-300) / <alpha-value>)",
          400: "rgb(var(--red-400) / <alpha-value>)",
          500: "rgb(var(--red-500) / <alpha-value>)",
          600: "rgb(var(--red-600) / <alpha-value>)",
          700: "rgb(var(--red-700) / <alpha-value>)",
          800: "rgb(var(--red-800) / <alpha-value>)",
          900: "rgb(var(--red-900) / <alpha-value>)",
        },
        amber: {
          50: "rgb(var(--amber-50) / <alpha-value>)",
          100: "rgb(var(--amber-100) / <alpha-value>)",
          200: "rgb(var(--amber-200) / <alpha-value>)",
          300: "rgb(var(--amber-300) / <alpha-value>)",
          400: "rgb(var(--amber-400) / <alpha-value>)",
          500: "rgb(var(--amber-500) / <alpha-value>)",
          600: "rgb(var(--amber-600) / <alpha-value>)",
          700: "rgb(var(--amber-700) / <alpha-value>)",
          800: "rgb(var(--amber-800) / <alpha-value>)",
          900: "rgb(var(--amber-900) / <alpha-value>)",
        },
        green: {
          50: "rgb(var(--green-50) / <alpha-value>)",
          100: "rgb(var(--green-100) / <alpha-value>)",
          500: "rgb(var(--green-500) / <alpha-value>)",
          600: "rgb(var(--green-600) / <alpha-value>)",
          700: "rgb(var(--green-700) / <alpha-value>)",
          800: "rgb(var(--green-800) / <alpha-value>)",
        },
        emerald: {
          500: "rgb(var(--emerald-500) / <alpha-value>)",
          600: "rgb(var(--emerald-600) / <alpha-value>)",
          700: "rgb(var(--emerald-700) / <alpha-value>)",
        },
        purple: {
          50: "rgb(var(--purple-50) / <alpha-value>)",
          100: "rgb(var(--purple-100) / <alpha-value>)",
          200: "rgb(var(--purple-200) / <alpha-value>)",
          500: "rgb(var(--purple-500) / <alpha-value>)",
          600: "rgb(var(--purple-600) / <alpha-value>)",
          700: "rgb(var(--purple-700) / <alpha-value>)",
        },

        // Roles: name the job, not a shade.
        surface: "rgb(var(--surface) / <alpha-value>)",
        canvas: "rgb(var(--surface-canvas) / <alpha-value>)",
        sunken: "rgb(var(--surface-sunken) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",
        // State fills and the disabled foreground: a subtle surface takes these on
        // hover/press, so no component has to invent an opacity mix.
        "surface-hover": "rgb(var(--surface-hover) / <alpha-value>)",
        "surface-pressed": "rgb(var(--surface-pressed) / <alpha-value>)",
        raised: "rgb(var(--surface-raised) / <alpha-value>)",
        "fg-disabled": "rgb(var(--fg-disabled) / <alpha-value>)",
        accent: "rgb(var(--accent) / <alpha-value>)",
        "on-accent": "rgb(var(--on-accent) / <alpha-value>)",
        danger: "rgb(var(--danger) / <alpha-value>)",

        // Kept names (the shell uses these three) pointing at the same roles.
        background: "rgb(var(--surface-canvas) / <alpha-value>)",
        "on-background": "rgb(var(--gray-900) / <alpha-value>)",
        "surface-container": "rgb(var(--surface) / <alpha-value>)",
      },
      spacing: {
        "sidebar-width": "240px",
        "panel-padding": "12px",
        "gutter": "16px",
        "unit": "4px",
        "margin": "24px",
        // The chrome's own heights, so a theme can set the density (Fluent's
        // header is taller than the shipped one) without editing the shell.
        chrome: "var(--chrome-h)",
        status: "var(--status-h)",
      },
      borderRadius: {
        DEFAULT: "var(--radius-control)",
        // A theme decides the corner language (Fluent is squarer than Tailwind's
        // defaults); the components only name the role.
        card: "var(--radius-card)",
        control: "var(--radius-control)",
        pill: "9999px",
      },
      boxShadow: {
        // Two elevation steps, so a card and a dialog differ by role, not by
        // whichever literal each component picked.
        card: "var(--shadow-card)",
        flyout: "var(--shadow-flyout)",
      },
      transitionDuration: {
        // Every existing `transition-*` utility follows the theme's scale.
        DEFAULT: "var(--motion-fast)",
        normal: "var(--motion-normal)",
      },
      transitionTimingFunction: {
        DEFAULT: "var(--motion-ease)",
      },
      fontFamily: {
        sans: ["var(--font-ui)"],
        mono: ["var(--font-mono)"],
        // Every family resolves through the theme, so a theme can swap the UI font
        // (e.g. Segoe UI Variable) without touching a component.
        kbd: ["var(--font-mono)"],
        "code-md": ["var(--font-mono)"],
        "code-sm": ["var(--font-mono)"],
        "body-md": ["var(--font-ui)"],
        "body-sm": ["var(--font-ui)"],
        "body-lg": ["var(--font-ui)"],
        "label-caps": ["var(--font-ui)"],
        "headline-lg": ["var(--font-ui)"],
        "headline-md": ["var(--font-ui)"],
        // Telemetry: Fluent sets numerals in Bahnschrift, which keeps the status
        // strip's digits aligned without a monospace face.
        numeric: ["var(--font-numeric)"],
      }
    },
  },
  plugins: [],
}