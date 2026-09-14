import { useId } from "react";
import {
  SurfacePanel,
  ToolbarButton,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";

export type ChatConnectionState =
  | "idle"
  | "connecting"
  | "streaming"
  | "waiting"
  | "error"
  | "stopped";

export type ChatConnectionStatusProps = {
  state: ChatConnectionState;
  detail?: string | null;
  className?: string;
  retryLabel?: string;
  cancelLabel?: string;
  onRetry?: () => void;
  onCancel?: () => void;
};

type StatusTone = "neutral" | "active" | "waiting" | "danger" | "stopped";

type StatusCopy = {
  label: string;
  description: string;
  tone: StatusTone;
};

const STATUS_COPY: Record<ChatConnectionState, StatusCopy> = {
  idle: {
    label: "Chat ready",
    description: "No active stream.",
    tone: "neutral",
  },
  connecting: {
    label: "Connecting",
    description: "Opening the chat stream.",
    tone: "active",
  },
  streaming: {
    label: "Streaming",
    description: "Response is arriving.",
    tone: "active",
  },
  waiting: {
    label: "Waiting",
    description: "Waiting for the next stream event.",
    tone: "waiting",
  },
  error: {
    label: "Connection error",
    description: "The chat stream ended with an error.",
    tone: "danger",
  },
  stopped: {
    label: "Stream stopped",
    description: "The chat stream was stopped.",
    tone: "stopped",
  },
};

const ACTIVE_STATES = new Set<ChatConnectionState>([
  "connecting",
  "streaming",
  "waiting",
]);

const RETRY_STATES = new Set<ChatConnectionState>(["error", "stopped"]);

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

export function ChatConnectionStatus({
  state,
  detail,
  className,
  retryLabel = "Retry",
  cancelLabel = "Cancel",
  onRetry,
  onCancel,
}: ChatConnectionStatusProps) {
  const detailId = useId();
  const copy = STATUS_COPY[state];
  const visibleDetail = detail ?? copy.description;
  const isError = state === "error";
  const showRetry = Boolean(onRetry) && RETRY_STATES.has(state);
  const showCancel = Boolean(onCancel) && ACTIVE_STATES.has(state);
  const ariaLive = state === "idle" ? "off" : isError ? "assertive" : "polite";

  return (
    <SurfacePanel
      as="section"
      className={cx(
        "flex flex-col gap-3 bg-runtime-bg/70 px-4 py-3 shadow-sm sm:flex-row sm:items-center sm:justify-between",
        className,
      )}
      role={isError ? "alert" : "status"}
      aria-live={ariaLive}
      aria-atomic="true"
      aria-describedby={visibleDetail ? detailId : undefined}
    >
      <div className="flex min-w-0 items-start gap-3">
        <StateBadge
          status={state}
          label={copy.label}
          tone={connectionStatusTone(copy.tone)}
          live={ACTIVE_STATES.has(state)}
          size="xs"
          className="mt-0.5 shrink-0"
        />
        <div className="min-w-0">
          {visibleDetail ? (
            <p
              id={detailId}
              className="text-xs leading-5 text-ink-muted"
            >
              {visibleDetail}
            </p>
          ) : null}
        </div>
      </div>

      {showRetry || showCancel ? (
        <div className="flex shrink-0 items-center gap-2">
          {showCancel ? (
            <ToolbarButton
              onClick={onCancel}
              size="xs"
              variant="secondary"
            >
              {cancelLabel}
            </ToolbarButton>
          ) : null}
          {showRetry ? (
            <ToolbarButton
              onClick={onRetry}
              size="xs"
              variant="danger"
            >
              {retryLabel}
            </ToolbarButton>
          ) : null}
        </div>
      ) : null}
    </SurfacePanel>
  );
}

function connectionStatusTone(tone: StatusTone): StatusBadgeTone {
  if (tone === "active") return "emerald";
  if (tone === "waiting") return "amber";
  if (tone === "danger") return "red";
  return "neutral";
}
