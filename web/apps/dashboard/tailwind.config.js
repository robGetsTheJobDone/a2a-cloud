/** @type {import('tailwindcss').Config} */
import designSystem from "@a2a/design-system/tailwind";

export default {
  presets: [designSystem],
  content: ["./index.html", "./src/**/*.{ts,tsx}", "../../packages/design-system/src/**/*.{ts,tsx}"],
  // Fonts, colors, radii, shadows, and the runtime/signal palette all come from the
  // @a2a/design-system preset. Do not re-declare fontFamily here — it would shadow the
  // preset's Geist / JetBrains Mono / IBM Plex stack (REDESIGN_ETHOS §2).
  theme: {
    extend: {},
  },
  plugins: [],
};
