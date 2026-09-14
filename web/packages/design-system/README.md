# @a2a/design-system

Shared Tailwind preset, CSS helpers, and React primitives for the combined agent-infrastructure design direction.

## Tailwind

CommonJS configs:

```js
const designSystem = require("@a2a/design-system/tailwind");

module.exports = {
  presets: [designSystem],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "../../packages/design-system/src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
```

ESM configs:

```js
import designSystem from "@a2a/design-system/tailwind";

export default {
  presets: [designSystem],
  content: [
    "./index.html",
    "./src/**/*.{ts,tsx}",
    "../../packages/design-system/src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
```

## CSS Helpers

Import once from an app entry file if you want the plain CSS helpers:

```ts
import "@a2a/design-system/styles.css";
```

Helpers include:

- `ds-theme`
- `ds-runtime-grid-bg`
- `ds-dot-grid-bg`
- `ds-volt-grid-bg`
- `ds-hero-readable-fade`
- `ds-section-fade`
- `ds-text-balance`
- `ds-respect-motion`

## React

```tsx
import { Button, CodeShell, ReceiptLedger, RuntimeSection, StatusPill } from "@a2a/design-system/react";

export function ExampleHero() {
  return (
    <RuntimeSection withGrid>
      <StatusPill pulse>deploy agents in minutes</StatusPill>
      <h1 className="mt-7 max-w-[12ch] text-[clamp(2.75rem,7vw,4.75rem)] font-semibold leading-[0.95] tracking-[-0.02em]">
        Deploy agents as <span className="bg-protocol-gradient bg-clip-text text-transparent">governed products.</span>
      </h1>
      <div className="mt-8 flex flex-wrap gap-3">
        <Button href="/install/sdk">Deploy free</Button>
        <Button href="/docs" variant="ghost">View docs</Button>
      </div>
      <CodeShell className="mt-8" title="ship in minutes">
        <pre>{`$ pipx install a2a-pack
$ a2a signup
$ a2a init my-agent
$ a2a deploy`}</pre>
      </CodeShell>
    </RuntimeSection>
  );
}
```

## Design Tokens

Use these Tailwind families:

- `runtime-*`: dark surfaces, borders, and panel layers.
- `ink-*`: text hierarchy.
- `brand-*`: A2A signal-field energy; use as a structural brand field, not copied chartreuse/yellow.
- `signal-protocol`: cyan runtime/API/protocol.
- `signal-live`: emerald live/success/receipt outcome.
- `signal-authority`: amber grants/permission.
- `signal-peer`: violet peer/provenance.
- `signal-proof`: fuchsia proof/audit/signature.
