import type { ReactNode } from "react";
import { StatusPill, type StatusPillTone } from "@a2a/design-system";

export type ChatEventTone = "neutral" | "blue" | "amber" | "emerald" | "red";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

// Single source of truth: every chat-event tone resolves to a Runtime-Telemetry
// signal token. blue=protocol (primary), amber=authority, emerald=live,
// red=danger, neutral=substrate. No neutral-*/sky-*/raw hex anywhere downstream.
const TONE_TO_PILL: Record<ChatEventTone, StatusPillTone> = {
  neutral: "neutral",
  blue: "protocol",
  amber: "authority",
  emerald: "live",
  red: "danger",
};

const CARD_TONE_CLASS: Record<ChatEventTone, string> = {
  neutral: "border-runtime-line-soft/70 bg-runtime-bg/90",
  blue: "border-signal-protocol/60 bg-signal-protocol/[0.04]",
  amber: "border-signal-authority/60 bg-signal-authority/[0.05]",
  emerald: "border-signal-live/60 bg-signal-live/[0.04]",
  red: "border-signal-danger/70 bg-signal-danger/[0.05]",
};

function chatEventPillTone(tone: ChatEventTone): StatusPillTone {
  return TONE_TO_PILL[tone];
}

export function ChatEventCard({
  tone = "neutral",
  children,
  className,
}: {
  tone?: ChatEventTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className="flex min-w-0 justify-start">
      <section
        className={cx(
          "w-full min-w-0 overflow-hidden rounded-md border p-4 sm:max-w-[92%]",
          CARD_TONE_CLASS[tone],
          className,
        )}
      >
        {children}
      </section>
    </div>
  );
}

export function ChatEventHeader({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      {children}
    </div>
  );
}

export function ChatEventStatus({
  tone,
  label,
  pulse = false,
}: {
  tone: ChatEventTone;
  label: string;
  pulse?: boolean;
}) {
  return (
    <StatusPill
      tone={chatEventPillTone(tone)}
      size="xs"
      pulse={pulse}
      className="font-semibold uppercase tracking-wider"
    >
      {label}
    </StatusPill>
  );
}

export function ChatEventMeta({ children }: { children: ReactNode }) {
  return (
    <span className="break-anywhere text-ink-muted normal-case tracking-normal">
      {children}
    </span>
  );
}

export function ChatEventActions({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("mt-4 flex flex-wrap items-center gap-2", className)}>
      {children}
    </div>
  );
}
