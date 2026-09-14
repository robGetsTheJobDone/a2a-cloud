import { useEffect, useId, useMemo, useState } from "react";
import {
  upsertConsumerSetup,
  upsertOrgConsumerSetup,
  type ConsumerSetupField,
  type ConsumerSetupStatus,
} from "../api";
import {
  ChatEventActions,
  ChatEventCard,
  ChatEventHeader,
  ChatEventMeta,
  ChatEventStatus,
} from "./ChatEventCard";

export type AgentSetupRequiredEvent = {
  kind: "setup_required";
  agent: string;
  skill: string;
  setup: ConsumerSetupStatus;
  missing_required: string[];
};

const FIELD_INPUT_BASE_CLASS =
  "mt-2 w-full rounded-md border bg-runtime-panel px-3 py-2 text-sm text-ink outline-none transition placeholder:text-ink-faint focus:border-signal-protocol disabled:cursor-not-allowed disabled:opacity-60";

const ACTION_BASE_CLASS =
  "inline-flex max-w-full items-center justify-center rounded-md px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-40";

const PRIMARY_ACTION_CLASS = `${ACTION_BASE_CLASS} bg-signal-protocol text-runtime-bg hover:bg-brand-field-hover`;

const SECONDARY_ACTION_CLASS = `${ACTION_BASE_CLASS} border border-runtime-line-soft/60 text-ink-soft hover:border-runtime-line-strong hover:text-ink`;

export function ChatAgentSetupRequiredCard({
  ev,
}: {
  ev: AgentSetupRequiredEvent;
}) {
  const componentId = useId();
  const [status, setStatus] = useState<ConsumerSetupStatus>(ev.setup);
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    initialSetupValues(ev.setup),
  );
  const [saving, setSaving] = useState<"user" | "org" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const fields = status.declaration.fields;
  const valueByName = useMemo(
    () => new Map(status.values.map((value) => [value.name, value])),
    [status.values],
  );
  const missing = useMemo(
    () => new Set(status.missing_required || ev.missing_required),
    [ev.missing_required, status.missing_required],
  );
  const marketplaceHref =
    `/marketplace?agent=${encodeURIComponent(ev.agent)}` +
    `&skill=${encodeURIComponent(ev.skill)}&intent=trial`;

  useEffect(() => {
    setStatus(ev.setup);
    setValues(initialSetupValues(ev.setup));
    setSaving(null);
    setErr(null);
    setSaved(null);
  }, [ev.agent, ev.skill, ev.setup]);

  async function save(scope: "user" | "org") {
    const payload = payloadSetupValues(fields, values);
    if (Object.keys(payload).length === 0) {
      setErr("Enter at least one setup value to save.");
      return;
    }
    setSaving(scope);
    setErr(null);
    setSaved(null);
    try {
      const next =
        scope === "org"
          ? await upsertOrgConsumerSetup(ev.agent, payload, status.organization?.slug)
          : await upsertConsumerSetup(ev.agent, payload);
      setStatus(next);
      setValues(initialSetupValues(next));
      setSaved(
        next.complete
          ? "Setup saved. Ask again to continue this agent handoff."
          : "Setup saved.",
      );
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setSaving(null);
    }
  }

  return (
    <ChatEventCard tone="amber">
      <ChatEventHeader>
        <ChatEventStatus
          tone="amber"
          label="Agent setup required"
          pulse={!status.complete}
        />
        <ChatEventMeta>
          <span className="font-mono">{ev.agent}.{ev.skill}</span>
        </ChatEventMeta>
        {fields.length > 0 && (
          <ChatEventMeta>
            {fields.length} setup {fields.length === 1 ? "field" : "fields"}
          </ChatEventMeta>
        )}
      </ChatEventHeader>

      <div className="mt-3 grid gap-3">
        {fields.map((field) => {
          const current = valueByName.get(field.name);
          const configured = Boolean(current?.configured);
          const fieldMissing = missing.has(field.name);
          const inputId = setupFieldInputId(componentId, field.name);
          return (
            <div key={field.name} className="min-w-0">
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                <label htmlFor={inputId} className="font-medium text-ink-soft">
                  {field.label || field.name}
                </label>
                <span className="font-mono text-ink-faint">{field.kind}</span>
                {field.required && <span className="text-signal-protocol">required</span>}
                {fieldMissing && <span className="text-signal-authority">missing</span>}
              </div>
              {field.description && (
                <div className="mt-1 text-xs leading-relaxed text-ink-muted">
                  {field.description}
                </div>
              )}
              {configured && (
                <div className="break-anywhere mt-1 font-mono text-[10px] text-ink-muted">
                  {current?.source || "configured"}:{" "}
                  {current?.value_redacted || "configured"}
                </div>
              )}
              <SetupRequiredFieldInput
                id={inputId}
                field={field}
                value={values[field.name]}
                invalid={fieldMissing}
                disabled={saving !== null}
                onChange={(value) => {
                  setValues((currentValues) => ({
                    ...currentValues,
                    [field.name]: value,
                  }));
                  setErr(null);
                  setSaved(null);
                }}
              />
            </div>
          );
        })}
      </div>

      {err && (
        <div
          role="alert"
          className="mt-3 rounded-md border border-signal-danger/50 bg-signal-danger/12 px-3 py-2 text-xs text-signal-danger"
        >
          {err}
        </div>
      )}
      {saved && (
        <div className="mt-3 rounded-md border border-signal-live/45 bg-signal-live/12 px-3 py-2 text-xs text-signal-live">
          {saved}
        </div>
      )}

      <ChatEventActions>
        <button
          type="button"
          onClick={() => save("user")}
          disabled={saving !== null || fields.length === 0}
          className={PRIMARY_ACTION_CLASS}
        >
          {saving === "user" ? "Saving..." : "Save for me"}
        </button>
        {status.can_manage_org && (
          <button
            type="button"
            onClick={() => save("org")}
            disabled={saving !== null || fields.length === 0}
            className={SECONDARY_ACTION_CLASS}
          >
            {saving === "org"
              ? "Saving..."
              : `Save for ${status.organization?.name || "org"}`}
          </button>
        )}
        <a
          href={marketplaceHref}
          className={SECONDARY_ACTION_CLASS}
        >
          Configure and run trial
        </a>
      </ChatEventActions>
    </ChatEventCard>
  );
}

function SetupRequiredFieldInput({
  id,
  field,
  value,
  invalid,
  disabled,
  onChange,
}: {
  id: string;
  field: ConsumerSetupField;
  value: unknown;
  invalid: boolean;
  disabled: boolean;
  onChange: (value: unknown) => void;
}) {
  const baseClass = `${FIELD_INPUT_BASE_CLASS} ${
    invalid ? "border-signal-authority/45" : "border-runtime-line-soft/60"
  }`;

  if (field.input_type === "boolean") {
    return (
      <select
        id={id}
        value={value === true ? "true" : value === false ? "false" : ""}
        onChange={(event) =>
          onChange(event.target.value === "" ? "" : event.target.value === "true")
        }
        disabled={disabled}
        aria-invalid={invalid || undefined}
        className={baseClass}
      >
        <option value="">Select</option>
        <option value="true">True</option>
        <option value="false">False</option>
      </select>
    );
  }

  if (field.input_type === "select") {
    return (
      <select
        id={id}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        className={baseClass}
      >
        <option value="">Select</option>
        {field.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }

  if (field.input_type === "textarea") {
    return (
      <textarea
        id={id}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        rows={3}
        className={baseClass}
      />
    );
  }

  return (
    <input
      id={id}
      type={field.kind === "secret" ? "password" : setupHtmlInputType(field.input_type)}
      value={typeof value === "string" || typeof value === "number" ? value : ""}
      onChange={(event) =>
        onChange(
          field.input_type === "number" && event.target.value !== ""
            ? Number(event.target.value)
            : event.target.value,
        )
      }
      disabled={disabled}
      aria-invalid={invalid || undefined}
      autoComplete={field.kind === "secret" ? "new-password" : "off"}
      className={baseClass}
    />
  );
}

export function initialSetupValues(
  status: ConsumerSetupStatus,
): Record<string, unknown> {
  const configured = new Set(
    status.values.filter((value) => value.configured).map((value) => value.name),
  );
  const out: Record<string, unknown> = {};
  for (const field of status.declaration.fields) {
    if (!configured.has(field.name)) out[field.name] = "";
  }
  return out;
}

export function payloadSetupValues(
  fields: ConsumerSetupField[],
  values: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of fields) {
    const value = values[field.name];
    if (value === undefined || value === null || value === "") continue;
    out[field.name] = value;
  }
  return out;
}

export function setupHtmlInputType(inputType: string) {
  if (["url", "email", "number"].includes(inputType)) return inputType;
  return "text";
}

function setupFieldInputId(componentId: string, field: string) {
  return `setup-${componentId}-${field}`.replace(/[^a-zA-Z0-9_-]/g, "-");
}

function messageFromError(ex: unknown) {
  return ex instanceof Error ? ex.message : String(ex);
}
