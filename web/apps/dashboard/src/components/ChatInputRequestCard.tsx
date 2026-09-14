import {
  submitInputRequest,
  type JsonSchema,
} from "../api";
import {
  ChatEventCard,
  ChatEventHeader,
  ChatEventMeta,
  ChatEventStatus,
  type ChatEventTone,
} from "./ChatEventCard";
import {
  countFileFields,
  inputRequestMeta,
} from "./chatInputRequestUtils";
import { ChatStructuredInputPanel } from "./ChatStructuredInputPanel";

export type ChatInputRequestEvent = {
  kind: "input";
  request_id: string;
  grant_id: string;
  title: string;
  reason: string;
  schema: JsonSchema;
  ui_schema: Record<string, unknown>;
  status: "pending" | "submitted" | "timeout";
  value_preview?: Record<string, unknown>;
};

export function ChatInputRequestCard({ ev }: { ev: ChatInputRequestEvent }) {
  const isPending = ev.status === "pending";

  async function submit(value: Record<string, unknown>) {
    if (!isPending) return;
    await submitInputRequest(ev.request_id, value);
  }

  const statusLabel =
    ev.status === "pending"
      ? "Agent needs structured input"
      : ev.status === "submitted"
        ? "Input submitted"
        : "Input timed out";
  const propertyCount = Object.keys(ev.schema.properties || {}).length;
  const requiredCount = ev.schema.required?.length || 0;
  const fileFieldCount = countFileFields(ev.schema, ev.ui_schema);
  const meta = inputRequestMeta(propertyCount, requiredCount, fileFieldCount);
  const tone: ChatEventTone =
    ev.status === "submitted" ? "emerald" : ev.status === "timeout" ? "red" : "amber";
  const title = ev.title || ev.schema.title || "More information needed";

  return (
    <ChatEventCard tone={tone}>
      <ChatEventHeader>
        <ChatEventStatus
          tone={tone}
          label={statusLabel}
          pulse={isPending}
        />
        <ChatEventMeta>
          req <span className="font-mono">{ev.request_id.slice(0, 10)}</span>
        </ChatEventMeta>
        {meta && <ChatEventMeta>{meta}</ChatEventMeta>}
      </ChatEventHeader>

      <div className="mt-3">
        <div className="break-anywhere text-sm font-medium text-ink">
          {title}
        </div>
        {ev.reason && (
          <div className="break-anywhere mt-1 text-sm text-ink-dim">
            {ev.reason}
          </div>
        )}
      </div>

      {isPending ? (
        <div className="mt-4">
          <ChatStructuredInputPanel
            schema={ev.schema}
            uiSchema={ev.ui_schema}
            requestId={ev.request_id}
            submitLabel="Submit"
            title={title}
            description={ev.reason}
            showHeader={false}
            className="border-runtime-line-soft/70 bg-runtime-bg/70"
            onSubmit={submit}
          />
        </div>
      ) : (
        <pre className="mt-3 max-h-48 overflow-auto rounded-md border border-runtime-line-soft/60 bg-runtime-panel/60 p-2 text-xs text-ink-soft">
          {JSON.stringify(ev.value_preview ?? {}, null, 2)}
        </pre>
      )}
    </ChatEventCard>
  );
}
