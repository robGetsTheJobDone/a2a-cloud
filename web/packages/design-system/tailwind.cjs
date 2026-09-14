/** @type {import("tailwindcss").Config} */
module.exports = {
  theme: {
    extend: {
      colors: {
        runtime: {
          bg: "#071b16",
          soft: "#092018",
          field: "#0d2a22",
          basin: "#12352b",
          trace: "#173f34",
          panel: "#0a211a",
          raised: "#102c23",
          high: "#17382e",
          line: "#1d4b3f",
          "line-soft": "#14382f",
          "line-mid": "#2a6354",
          "line-strong": "#5d8a7c",
          "mint-line": "#286f5e",
        },
        ink: {
          DEFAULT: "#f7fff8",
          soft: "#dcebe3",
          dim: "#adc7bd",
          muted: "#789286",
          faint: "#557165",
          paper: "#f3f7f2",
          "paper-soft": "#e8f3ec",
          "paper-card": "#fbfff8",
          "paper-line": "#c5dbd0",
          black: "#202020",
        },
        brand: {
          volt: "#b8ffe0",
          "field-hover": "#8ff7e4",
          lilac: "#d9b8ff",
        },
        signal: {
          protocol: "#7ee7d0",
          "protocol-strong": "#2dd6b3",
          action: "#4f8cff",
          live: "#8bdc9e",
          authority: "#f3d36b",
          peer: "#c2a6ff",
          proof: "#ff8db3",
          danger: "#ff5c7a",
        },
      },
      fontFamily: {
        sans: ["Geist", "Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "IBM Plex Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
        industrial: ["IBM Plex Sans", "Geist", "Inter", "system-ui", "sans-serif"],
        editorial: ["GT Sectra", "Georgia", "Times New Roman", "serif"],
      },
      fontSize: {
        "runtime-xs": ["0.6875rem", { lineHeight: "1rem" }],
        "runtime-sm": ["0.8125rem", { lineHeight: "1.125rem" }],
        "runtime-base": ["1rem", { lineHeight: "1.55" }],
        "runtime-lg": ["1.125rem", { lineHeight: "1.55" }],
        "runtime-xl": ["1.25rem", { lineHeight: "1.4" }],
        "runtime-h6": ["1rem", { lineHeight: "1.25" }],
        "runtime-h5": ["1.25rem", { lineHeight: "1.2" }],
        "runtime-h4": ["1.5rem", { lineHeight: "1.15" }],
        "runtime-h3": ["2rem", { lineHeight: "1.1" }],
        "runtime-h2": ["3rem", { lineHeight: "1.05", letterSpacing: "-0.02em" }],
        "runtime-h1": ["4.75rem", { lineHeight: "0.95", letterSpacing: "-0.02em" }],
      },
      borderRadius: {
        "runtime-xs": "0.25rem",
        "runtime-sm": "0.375rem",
        "runtime-md": "0.5rem",
        "runtime-lg": "0.75rem",
      },
      boxShadow: {
        "glow-cyan": "0 0 50px -32px rgba(126, 231, 208, 0.55)",
        "glow-live": "0 0 32px -20px rgba(139, 220, 158, 0.7)",
        "glow-peer": "0 0 50px -28px rgba(194, 166, 255, 0.55)",
        "hard-ink": "4px 4px 0 0 #202020",
        "hard-mint": "4px 4px 0 0 #286f5e",
      },
      backgroundImage: {
        "runtime-grid":
          "radial-gradient(ellipse 70% 50% at 50% 0%, rgba(126, 231, 208, 0.11), transparent 70%), radial-gradient(ellipse 60% 40% at 80% 30%, rgba(194, 166, 255, 0.07), transparent 70%), radial-gradient(rgba(255, 255, 255, 0.045) 1px, transparent 1px)",
        "dot-grid": "radial-gradient(rgba(255, 255, 255, 0.04) 1px, transparent 1px)",
        "volt-grid":
          "linear-gradient(to right, rgba(7, 27, 22, 0.16) 1px, transparent 1px), linear-gradient(to bottom, rgba(7, 27, 22, 0.16) 1px, transparent 1px)",
        "hero-fade":
          "linear-gradient(90deg, #071b16 0%, rgba(7, 27, 22, 0.96) 34%, rgba(7, 27, 22, 0.62) 62%, rgba(7, 27, 22, 0.08) 100%)",
        "protocol-gradient": "linear-gradient(110deg, #7ee7d0 0%, #c2a6ff 100%)",
      },
      backgroundSize: {
        "runtime-grid-size": "auto, auto, 28px 28px",
        "dot-grid-size": "28px 28px",
        "volt-grid-size": "96px 96px",
      },
      maxWidth: {
        "runtime-sm": "48rem",
        "runtime-md": "64rem",
        "runtime-lg": "72rem",
        "runtime-xl": "88rem",
      },
      transitionTimingFunction: {
        telemetry: "cubic-bezier(0.4, 0, 0.2, 1)",
      },
      keyframes: {
        "telemetry-pulse": {
          "0%, 100%": { opacity: "0.65", transform: "scale(1)" },
          "50%": { opacity: "1", transform: "scale(1.08)" },
        },
        "graph-ping": {
          "0%": { opacity: "0.7", transform: "scale(1)" },
          "75%, 100%": { opacity: "0", transform: "scale(2)" },
        },
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        scan: {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(100%)" },
        },
      },
      animation: {
        "telemetry-pulse": "telemetry-pulse 2.4s ease-in-out infinite",
        "graph-ping": "graph-ping 2.2s cubic-bezier(0, 0, 0.2, 1) infinite",
        "fade-up": "fade-up 0.6s ease-out both",
        scan: "scan 4s linear infinite",
      },
    },
  },
};
