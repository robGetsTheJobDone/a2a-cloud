/** @type {import('tailwindcss').Config} */
const designSystem = require("@a2a/design-system/tailwind");

module.exports = {
  presets: [designSystem],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "../../packages/design-system/src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      typography: ({ theme }) => ({
        DEFAULT: {
          css: { color: theme("colors.neutral.200") },
        },
      }),
    },
  },
  plugins: [],
};
