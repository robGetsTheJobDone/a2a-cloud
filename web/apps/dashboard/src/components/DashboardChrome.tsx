import type {
  AnchorHTMLAttributes,
  ButtonHTMLAttributes,
  HTMLAttributes,
  InputHTMLAttributes,
  MouseEvent,
  ReactNode,
  RefObject,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { forwardRef, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { StatusPill, type StatusPillTone } from "@a2a/design-system";
import { Icon } from "./Icon";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

type PageShellWidth = "sm" | "md" | "lg" | "xl" | "full";
type PageShellLayout = "stack" | "full-height";

const pageShellWidthClasses: Record<PageShellWidth, string> = {
  sm: "max-w-3xl",
  md: "max-w-5xl",
  lg: "max-w-6xl",
  xl: "max-w-7xl",
  full: "max-w-none",
};

type ChromeSurfaceTone =
  | "panel"
  | "solid"
  | "table"
  | "empty"
  | "interactive"
  | "selected";

const chromeSurfaceBaseClass = "rounded-lg border ring-1";

const chromeSurfaceToneClasses: Record<ChromeSurfaceTone, string> = {
  panel: "border-runtime-line-soft/70 bg-runtime-panel/35 ring-white/[0.025]",
  solid: "border-runtime-line-soft/70 bg-runtime-bg/75 ring-white/[0.02]",
  table: "border-runtime-line-soft/70 bg-runtime-bg/50 ring-white/[0.02]",
  empty: "border-dashed border-runtime-line-soft/80 bg-runtime-panel/25 ring-white/[0.015]",
  interactive:
    "border-runtime-line-soft/70 bg-runtime-bg/70 ring-white/[0.02] hover:border-runtime-line-mid hover:bg-runtime-panel/55",
  selected: "border-signal-protocol/45 bg-runtime-panel/80 ring-signal-protocol/15",
};

function chromeSurfaceClass(tone: ChromeSurfaceTone = "panel", className?: string) {
  return cx(chromeSurfaceBaseClass, chromeSurfaceToneClasses[tone], className);
}

export type PageShellProps = HTMLAttributes<HTMLElement> & {
  as?: "main" | "section" | "div";
  eyebrow?: string;
  title?: string;
  description?: ReactNode;
  actions?: ReactNode;
  maxWidth?: PageShellWidth;
  layout?: PageShellLayout;
  children: ReactNode;
};

export function PageShell({
  as: Component = "main",
  eyebrow,
  title,
  description,
  actions,
  maxWidth = "lg",
  layout = "stack",
  className,
  children,
  ...mainProps
}: PageShellProps) {
  const hasHeader = Boolean(eyebrow || title || description || actions);

  return (
    <Component
      {...mainProps}
      className={cx(
        layout === "full-height"
          ? "mx-auto grid min-h-0 w-full flex-1 grid-rows-[auto_auto_minmax(0,1fr)] gap-4 overflow-hidden px-4 py-4 sm:px-6 sm:py-5"
          : "mx-auto w-full space-y-6 px-4 py-5 sm:px-6 sm:py-6",
        pageShellWidthClasses[maxWidth],
        className,
      )}
    >
      {hasHeader && (
        <header className="flex flex-col gap-4 border-b border-runtime-line-soft/70 pb-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            {eyebrow && (
              <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-muted">
                {eyebrow}
              </div>
            )}
            {title && (
              <h1 className="mt-1 text-xl font-semibold leading-tight text-ink sm:text-2xl">
                {title}
              </h1>
            )}
            {description && (
              <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-ink-dim">
                {description}
              </p>
            )}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </Component>
  );
}

export type FullHeightRouteFrameProps = HTMLAttributes<HTMLElement> & {
  as?: "main" | "section" | "div";
  children: ReactNode;
};

export function FullHeightRouteFrame({
  as: Component = "main",
  className,
  children,
  ...frameProps
}: FullHeightRouteFrameProps) {
  return (
    <Component
      {...frameProps}
      className={cx("min-h-0 flex-1 overflow-hidden bg-runtime-bg", className)}
    >
      {children}
    </Component>
  );
}

export type ScrollRouteFrameProps = HTMLAttributes<HTMLElement> & {
  as?: "main" | "section" | "div";
  children: ReactNode;
};

export function ScrollRouteFrame({
  as: Component = "div",
  className,
  children,
  ...frameProps
}: ScrollRouteFrameProps) {
  return (
    <Component
      {...frameProps}
      className={cx("min-h-0 flex-1 overflow-auto bg-runtime-bg", className)}
    >
      {children}
    </Component>
  );
}

export type PersistentRoutePanelProps = HTMLAttributes<HTMLDivElement> & {
  active: boolean;
  children: ReactNode;
};

export function PersistentRoutePanel({
  active,
  className,
  children,
  ...panelProps
}: PersistentRoutePanelProps) {
  const [mounted, setMounted] = useState(active);

  useEffect(() => {
    if (active) setMounted(true);
  }, [active]);

  if (!mounted) return null;

  return (
    <div
      {...panelProps}
      hidden={!active}
      className={cx("min-w-0", className)}
    >
      {children}
    </div>
  );
}

/**
 * Status tone vocabulary shared across the dashboard. Retained as the canonical
 * four-value semantic palette that derives from runtime status strings; chips
 * themselves render through the @a2a/design-system StatusPill (the retired
 * StatusBadge/StateBadge fork has been replaced by ./StatusPillAdapters).
 */
export type StatusBadgeTone =
  | "neutral"
  | "emerald"
  | "amber"
  | "red";

const statusBadgeToneToPillTone: Record<StatusBadgeTone, StatusPillTone> = {
  neutral: "neutral",
  emerald: "live",
  amber: "authority",
  red: "danger",
};

const successStatusValues = new Set([
  "complete",
  "completed",
  "configured",
  "deployed",
  "done",
  "enabled",
  "fulfilled",
  "healthy",
  "live",
  "merged",
  "ok",
  "passed",
  "provisioned",
  "ready",
  "success",
  "succeeded",
  "verified",
]);

const failureStatusValues = new Set([
  "blocked",
  "cancelled",
  "canceled",
  "denied",
  "disabled",
  "error",
  "failed",
  "failure",
  "revoked",
  "unhealthy",
]);

const attentionStatusValues = new Set([
  "active",
  "building",
  "claimed",
  "degraded",
  "deploying",
  "dispatching",
  "evaluating",
  "executing",
  "open",
  "pending",
  "planned",
  "planning",
  "provisioning",
  "queued",
  "running",
  "uploading",
  "waiting",
]);

const liveStatusValues = new Set([
  "active",
  "building",
  "deploying",
  "dispatching",
  "evaluating",
  "executing",
  "pending",
  "planning",
  "provisioning",
  "queued",
  "ready",
  "running",
  "uploading",
  "waiting",
]);

function normalizeStatusValue(status: string | null | undefined) {
  return String(status || "").trim().toLowerCase();
}

export function stateStatusTone(status: string | null | undefined): StatusBadgeTone {
  const normalized = normalizeStatusValue(status);
  if (successStatusValues.has(normalized)) return "emerald";
  if (failureStatusValues.has(normalized)) return "red";
  if (attentionStatusValues.has(normalized)) return "amber";
  return "neutral";
}

export function isLiveStateStatus(status: string | null | undefined) {
  return liveStatusValues.has(normalizeStatusValue(status));
}

const progressBarToneClasses: Record<StatusBadgeTone, string> = {
  neutral: "bg-ink-muted",
  emerald: "bg-signal-live",
  amber: "bg-signal-authority",
  red: "bg-signal-danger",
};

export function ProgressBar({
  value,
  tone = "neutral",
  className,
  ...divProps
}: {
  value: number;
  tone?: StatusBadgeTone;
} & HTMLAttributes<HTMLDivElement>) {
  const boundedValue = Number.isFinite(value)
    ? Math.max(0, Math.min(100, value))
    : 0;
  return (
    <div
      {...divProps}
      role={divProps.role ?? "progressbar"}
      aria-valuemin={divProps["aria-valuemin"] ?? 0}
      aria-valuemax={divProps["aria-valuemax"] ?? 100}
      aria-valuenow={divProps["aria-valuenow"] ?? Math.round(boundedValue)}
      className={cx("h-1.5 overflow-hidden rounded-full bg-runtime-raised", className)}
    >
      <div
        className={cx("h-full rounded-full", progressBarToneClasses[tone])}
        style={{ width: `${Math.max(2, boundedValue)}%` }}
      />
    </div>
  );
}

export type ToolbarButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "success"
  | "danger";
export type ToolbarButtonSize = "xs" | "sm" | "md";

const toolbarButtonVariantClasses: Record<ToolbarButtonVariant, string> = {
  primary:
    "border-signal-protocol/80 bg-signal-protocol text-runtime-bg shadow-sm shadow-black/40 hover:border-brand-field-hover hover:bg-brand-field-hover focus-visible:ring-signal-protocol/40",
  secondary:
    "border-runtime-line/80 bg-runtime-raised/60 text-ink-soft shadow-sm shadow-black/10 hover:border-runtime-line-strong hover:bg-runtime-raised hover:text-ink focus-visible:ring-ink-muted/40",
  ghost:
    "border-transparent bg-transparent text-ink-dim hover:bg-runtime-panel/80 hover:text-ink focus-visible:ring-ink-muted/40",
  success:
    "border-signal-live/45 bg-signal-live/12 text-signal-live hover:border-signal-live/70 hover:bg-signal-live/20 focus-visible:ring-signal-live/40",
  danger:
    "border-signal-danger/50 bg-signal-danger/12 text-signal-danger hover:border-signal-danger/70 hover:bg-signal-danger/20 focus-visible:ring-signal-danger/40",
};

const toolbarButtonSizeClasses: Record<ToolbarButtonSize, string> = {
  xs: "h-7 px-2 text-[11px]",
  sm: "h-8 px-2.5 text-xs",
  md: "h-9 px-3 text-sm",
};

function toolbarControlClass({
  variant,
  size,
  active,
  className,
}: {
  variant: ToolbarButtonVariant;
  size: ToolbarButtonSize;
  active?: boolean;
  className?: string;
}) {
  return cx(
    "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg border font-medium transition duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-runtime-bg disabled:cursor-not-allowed disabled:opacity-40",
    toolbarButtonVariantClasses[variant],
    toolbarButtonSizeClasses[size],
    active && "border-signal-protocol/50 bg-signal-protocol/15 text-signal-protocol",
    className,
  );
}

export type ToolbarButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ToolbarButtonVariant;
  size?: ToolbarButtonSize;
  active?: boolean;
};

export const ToolbarButton = forwardRef<HTMLButtonElement, ToolbarButtonProps>(function ToolbarButton({
  variant = "secondary",
  size = "sm",
  active,
  type = "button",
  className,
  children,
  ...buttonProps
}: ToolbarButtonProps, ref) {
  return (
    <button
      {...buttonProps}
      ref={ref}
      type={type}
      aria-pressed={active ?? buttonProps["aria-pressed"]}
      data-active={active ? "true" : undefined}
      className={toolbarControlClass({ variant, size, active, className })}
    >
      {children}
    </button>
  );
});

export type ToolbarLinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  href: string;
  variant?: ToolbarButtonVariant;
  size?: ToolbarButtonSize;
  active?: boolean;
  external?: boolean;
};

export function ToolbarLink({
  href,
  variant = "secondary",
  size = "sm",
  active,
  external,
  className,
  children,
  target,
  rel,
  ...anchorProps
}: ToolbarLinkProps) {
  const isInternal = !external && href.startsWith("/");
  const ariaCurrent = active ? "page" : anchorProps["aria-current"];
  const controlClassName = toolbarControlClass({
    variant,
    size,
    active,
    className,
  });

  if (isInternal) {
    return (
      <Link
        {...anchorProps}
        to={href}
        aria-current={ariaCurrent}
        className={controlClassName}
      >
        {children}
      </Link>
    );
  }

  return (
    <a
      {...anchorProps}
      href={href}
      target={external ? "_blank" : target}
      rel={external ? "noreferrer" : rel}
      aria-current={ariaCurrent}
      className={controlClassName}
    >
      {children}
    </a>
  );
}

export type CopyButtonProps = Omit<ToolbarButtonProps, "onClick"> & {
  value: string;
  label?: string;
  copiedLabel?: string;
  onCopied?: () => void;
  onCopyError?: (error: unknown) => void;
};

export function CopyButton({
  value,
  label = "Copy",
  copiedLabel = "Copied",
  onCopied,
  onCopyError,
  size = "xs",
  variant = "secondary",
  children,
  disabled,
  ...buttonProps
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  async function onCopy() {
    try {
      await copyTextToClipboard(value);
      setCopied(true);
      onCopied?.();
      window.setTimeout(() => setCopied(false), 1400);
    } catch (error) {
      onCopyError?.(error);
    }
  }

  return (
    <ToolbarButton
      {...buttonProps}
      type="button"
      onClick={onCopy}
      disabled={disabled || !value}
      aria-label={buttonProps["aria-label"] ?? label}
      title={buttonProps.title ?? label}
      size={size}
      variant={variant}
    >
      {copied ? copiedLabel : children ?? "Copy"}
    </ToolbarButton>
  );
}

type CopyValueFieldLayout = "stack" | "inline";

export type CopyValueFieldProps = {
  label: string;
  value: ReactNode;
  copyValue?: string | null;
  multiline?: boolean;
  layout?: CopyValueFieldLayout;
  className?: string;
  codeClassName?: string;
};

export function CopyValueField({
  label,
  value,
  copyValue,
  multiline = false,
  layout = "stack",
  className,
  codeClassName,
}: CopyValueFieldProps) {
  const resolvedCopyValue =
    copyValue === undefined && typeof value === "string" ? value : copyValue;
  const canCopy = Boolean(resolvedCopyValue && resolvedCopyValue !== "pending");

  if (layout === "inline") {
    return (
      <div
        className={cx(
          "flex min-w-0 items-center gap-2 rounded-md bg-runtime-panel/60 px-2 py-1.5",
          className,
        )}
      >
        <div className="w-20 shrink-0 text-[11px] font-medium uppercase text-ink-muted">
          {label}
        </div>
        <code
          className={cx(
            "min-w-0 flex-1 truncate text-xs text-ink-soft",
            codeClassName,
          )}
        >
          {value}
        </code>
        {canCopy && (
          <CopyButton
            value={resolvedCopyValue || ""}
            label={`Copy ${label}`}
            size="xs"
            className="h-7 px-2"
          />
        )}
      </div>
    );
  }

  return (
    <div className={cx("min-w-0", className)}>
      <div className="mb-1 text-[10px] font-medium uppercase text-ink-faint">
        {label}
      </div>
      <div className="flex min-w-0 flex-col items-stretch gap-2 sm:flex-row sm:items-start">
        <code
          className={cx(
            "min-h-9 min-w-0 flex-1 rounded-md border border-runtime-line-soft/60 bg-runtime-bg px-2.5 py-2 text-xs text-ink-soft",
            multiline ? "whitespace-pre-wrap break-all" : "truncate",
            codeClassName,
          )}
        >
          {value}
        </code>
        {canCopy && (
          <CopyButton
            value={resolvedCopyValue || ""}
            label={`Copy ${label}`}
            size="md"
          />
        )}
      </div>
    </div>
  );
}

export function CopyValueRow({
  codeClassName,
  ...props
}: Omit<CopyValueFieldProps, "layout">) {
  return (
    <CopyValueField
      {...props}
      layout="inline"
      codeClassName={cx("font-mono", codeClassName)}
    />
  );
}

export type ReadOnlyValueFieldProps = Omit<CopyValueFieldProps, "copyValue"> & {
  copyable?: boolean;
  copyValue?: string | null;
};

export function ReadOnlyValueField({
  value,
  copyable = false,
  copyValue,
  ...props
}: ReadOnlyValueFieldProps) {
  const resolvedCopyValue =
    copyValue !== undefined
      ? copyValue
      : copyable && typeof value === "string"
        ? value
        : null;

  return (
    <CopyValueField
      {...props}
      value={value}
      copyValue={resolvedCopyValue}
    />
  );
}

export async function copyTextToClipboard(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("Clipboard unavailable");
}

export type FormFieldProps = {
  label: ReactNode;
  description?: ReactNode;
  className?: string;
  children: ReactNode;
};

export function FormField({
  label,
  description,
  className,
  children,
}: FormFieldProps) {
  return (
    <label className={cx("block space-y-1", className)}>
      <span className="text-xs font-medium text-ink-dim">{label}</span>
      {children}
      {description && (
        <span className="block text-xs leading-relaxed text-ink-muted">
          {description}
        </span>
      )}
    </label>
  );
}

export type ToggleFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "onChange"> & {
  label: ReactNode;
  description?: ReactNode;
  onCheckedChange: (checked: boolean) => void;
};

export function ToggleField({
  label,
  description,
  checked,
  disabled,
  onCheckedChange,
  className,
  ...inputProps
}: ToggleFieldProps) {
  return (
    <label
      className={cx(
        "flex items-center justify-between gap-3 rounded-md border border-runtime-line-soft/60 px-3 py-2 text-sm text-ink-soft transition",
        disabled
          ? "cursor-not-allowed opacity-50"
          : "cursor-pointer hover:border-runtime-line-mid hover:bg-runtime-panel/30",
        className,
      )}
    >
      <span className="min-w-0">
        <span className="block truncate">{label}</span>
        {description && (
          <span className="mt-0.5 block text-xs leading-relaxed text-ink-faint">
            {description}
          </span>
        )}
      </span>
      <input
        {...inputProps}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onCheckedChange(event.target.checked)}
        className="h-4 w-4 shrink-0 accent-signal-protocol"
      />
    </label>
  );
}

type FormControlOptions = {
  attention?: boolean;
  compact?: boolean;
  invalid?: boolean;
  mono?: boolean;
};

function formControlClass({
  attention,
  compact,
  invalid,
  mono,
  multiline,
  className,
}: FormControlOptions & { multiline?: boolean; className?: string }) {
  return cx(
    "w-full rounded-md border bg-runtime-bg/90 text-ink outline-none transition placeholder:text-ink-faint focus:border-signal-protocol focus:ring-2 focus:ring-signal-protocol/20 disabled:cursor-not-allowed disabled:opacity-50",
    multiline
      ? cx("min-h-24 resize-y", compact ? "px-2.5 py-2 text-xs" : "px-3 py-2 text-sm")
      : compact
        ? "h-8 px-2.5 text-xs"
        : "h-9 px-3 text-sm",
    invalid
      ? "border-signal-danger/50"
      : attention
        ? "border-signal-authority/45"
        : "border-runtime-line-soft/70",
    mono && "font-mono",
    className,
  );
}

export type TextInputProps = InputHTMLAttributes<HTMLInputElement> &
  FormControlOptions;

export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput({
  attention,
  compact,
  invalid,
  mono,
  className,
  type = "text",
  ...inputProps
}: TextInputProps, ref) {
  return (
    <input
      {...inputProps}
      ref={ref}
      type={type}
      className={formControlClass({ attention, compact, invalid, mono, className })}
    />
  );
});

export type TextAreaProps = TextareaHTMLAttributes<HTMLTextAreaElement> &
  FormControlOptions;

export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea({
  attention,
  compact,
  invalid,
  mono,
  className,
  ...textareaProps
}: TextAreaProps, ref) {
  return (
    <textarea
      {...textareaProps}
      ref={ref}
      className={formControlClass({
        attention,
        compact,
        invalid,
        mono,
        multiline: true,
        className,
      })}
    />
  );
});

export type SelectInputProps = SelectHTMLAttributes<HTMLSelectElement> &
  Pick<FormControlOptions, "attention" | "compact" | "invalid">;

export const SelectInput = forwardRef<HTMLSelectElement, SelectInputProps>(function SelectInput({
  attention,
  compact,
  invalid,
  className,
  ...selectProps
}: SelectInputProps, ref) {
  return (
    <select
      {...selectProps}
      ref={ref}
      className={formControlClass({ attention, compact, invalid, className })}
    />
  );
});

export type TabPillProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  selected?: boolean;
};

function tabControlClass(selected: boolean, className?: string) {
  return cx(
    "inline-flex h-8 shrink-0 items-center justify-center whitespace-nowrap rounded-md px-3 text-xs font-medium transition duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/40 focus-visible:ring-offset-2 focus-visible:ring-offset-runtime-bg",
    selected
      ? "bg-runtime-bg text-ink shadow-sm shadow-black/20 ring-1 ring-signal-protocol/35"
      : "text-ink-dim hover:bg-runtime-bg/70 hover:text-ink",
    className,
  );
}

export function TabPill({
  selected = false,
  type = "button",
  className,
  children,
  ...buttonProps
}: TabPillProps) {
  return (
    <button
      {...buttonProps}
      type={type}
      role={buttonProps.role ?? "tab"}
      aria-selected={selected}
      className={tabControlClass(selected, cx("disabled:cursor-not-allowed disabled:opacity-40", className))}
    >
      {children}
    </button>
  );
}

export type TabLinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  href: string;
  selected?: boolean;
};

export function TabLink({
  href,
  selected = false,
  className,
  children,
  ...anchorProps
}: TabLinkProps) {
  const ariaCurrent = selected ? "page" : anchorProps["aria-current"];
  const linkClassName = tabControlClass(selected, className);

  if (href.startsWith("/")) {
    return (
      <Link
        {...anchorProps}
        to={href}
        role={anchorProps.role ?? "tab"}
        aria-selected={selected}
        aria-current={ariaCurrent}
        className={linkClassName}
      >
        {children}
      </Link>
    );
  }

  return (
    <a
      {...anchorProps}
      href={href}
      role={anchorProps.role ?? "tab"}
      aria-selected={selected}
      aria-current={ariaCurrent}
      className={linkClassName}
    >
      {children}
    </a>
  );
}

export type SegmentedControlProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
};

export function SegmentedControl({
  className,
  children,
  role = "group",
  ...divProps
}: SegmentedControlProps) {
  return (
    <div
      {...divProps}
      role={role}
      className={chromeSurfaceClass(
        "panel",
        cx("inline-flex max-w-full overflow-x-auto p-1", className),
      )}
    >
      {children}
    </div>
  );
}

export type SegmentedButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  selected?: boolean;
};

export function SegmentedButton({
  selected = false,
  type = "button",
  className,
  children,
  ...buttonProps
}: SegmentedButtonProps) {
  return (
    <button
      {...buttonProps}
      type={type}
      aria-pressed={buttonProps["aria-pressed"] ?? selected}
      className={cx(
        "inline-flex h-8 shrink-0 items-center justify-center whitespace-nowrap rounded-md px-3 text-sm font-medium transition duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-muted/40 focus-visible:ring-offset-2 focus-visible:ring-offset-runtime-bg disabled:cursor-not-allowed disabled:opacity-40",
        selected
          ? "bg-runtime-bg text-ink shadow-sm shadow-black/20 ring-1 ring-runtime-line"
          : "text-ink-dim hover:bg-runtime-bg/70 hover:text-ink",
        className,
      )}
    >
      {children}
    </button>
  );
}

type DialogSize = "sm" | "md" | "lg" | "xl";

const dialogSizeClasses: Record<DialogSize, string> = {
  sm: "max-w-md",
  md: "max-w-2xl",
  lg: "max-w-4xl",
  xl: "max-w-6xl",
};

export type DialogProps = {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  onClose?: () => void;
  closeLabel?: string;
  size?: DialogSize;
  /** "center" = centered modal (default); "right" = full-height side sheet for
   *  detail/edit flows layered over a list context (mandate C). */
  placement?: "center" | "right";
  className?: string;
  initialFocusRef?: RefObject<HTMLElement>;
};

export function Dialog({
  open,
  title,
  description,
  actions,
  children,
  onClose,
  closeLabel = "Close dialog",
  size = "md",
  placement = "center",
  className,
  initialFocusRef,
}: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || !onClose) {
      return undefined;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const timeoutId = window.setTimeout(() => {
      (initialFocusRef?.current ?? panelRef.current)?.focus();
    }, 0);

    return () => {
      window.clearTimeout(timeoutId);
      previousFocus?.focus();
    };
  }, [initialFocusRef, open]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open) {
    return null;
  }

  const handleBackdropMouseDown = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) {
      onClose?.();
    }
  };

  return (
    <div
      className={cx(
        "fixed inset-0 z-50 flex bg-black/60 backdrop-blur-sm",
        placement === "right" ? "items-stretch justify-end" : "items-center justify-center p-4",
      )}
      onMouseDown={handleBackdropMouseDown}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cx(
          "flex w-full flex-col overflow-hidden border border-runtime-line-soft/80 bg-runtime-panel/95 shadow-2xl shadow-black/60 ring-1 ring-white/5 outline-none backdrop-blur-xl",
          placement === "right"
            ? "h-full max-h-screen rounded-l-2xl border-y-0 border-r-0 sheet-in-right"
            : "max-h-[92vh] rounded-lg",
          dialogSizeClasses[size],
          className,
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-runtime-line-soft/60 px-5 py-4">
          <div className="min-w-0">
            <h2 id={titleId} className="text-sm font-semibold text-ink">
              {title}
            </h2>
            {description && (
              <p id={descriptionId} className="mt-1 text-xs leading-relaxed text-ink-muted">
                {description}
              </p>
            )}
          </div>
          {onClose && (
            <button
              type="button"
              aria-label={closeLabel}
              className="-mr-1 -mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-ink-muted transition hover:bg-runtime-raised hover:text-ink-soft focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-muted/40"
              onClick={onClose}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M18 6 6 18M6 6l12 12" />
              </svg>
            </button>
          )}
        </header>
        <div className="min-h-0 overflow-auto px-5 py-5">{children}</div>
        {actions && (
          <footer className="flex justify-end gap-2 border-t border-runtime-line-soft/60 px-5 py-3.5">
            {actions}
          </footer>
        )}
      </div>
    </div>
  );
}

type LiveRegionPoliteness = "polite" | "assertive";

export type LiveRegionProps = HTMLAttributes<HTMLDivElement> & {
  politeness?: LiveRegionPoliteness;
  atomic?: boolean;
  visuallyHidden?: boolean;
};

export function LiveRegion({
  politeness = "polite",
  atomic = true,
  visuallyHidden = true,
  className,
  children,
  ...divProps
}: LiveRegionProps) {
  return (
    <div
      {...divProps}
      role={divProps.role ?? (politeness === "assertive" ? "alert" : "status")}
      aria-live={politeness}
      aria-atomic={atomic}
      className={cx(visuallyHidden && "sr-only", className)}
    >
      {children}
    </div>
  );
}

export function SectionPanel({
  title,
  description,
  actions,
  className,
  children,
  ...sectionProps
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
} & Omit<HTMLAttributes<HTMLElement>, "title">) {
  return (
    <section
      {...sectionProps}
      className={chromeSurfaceClass("panel", className)}
    >
      <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 px-4 py-3.5 sm:flex-row sm:items-start sm:justify-between sm:px-5">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          {description && (
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              {description}
            </p>
          )}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
      </div>
      <div className="p-4 sm:p-5">{children}</div>
    </section>
  );
}

export function SurfacePanel({
  as: Component = "section",
  className,
  children,
  ...props
}: {
  as?: "div" | "section" | "article" | "aside" | "dl" | "li" | "label" | "fieldset";
  children: ReactNode;
} & HTMLAttributes<HTMLElement>) {
  return (
    <Component
      {...props}
      className={chromeSurfaceClass("panel", className)}
    >
      {children}
    </Component>
  );
}

export type CodeBlockProps = HTMLAttributes<HTMLPreElement> & {
  label?: ReactNode;
  wrap?: boolean;
};

export function CodeBlock({
  label,
  wrap = true,
  className,
  children,
  ...preProps
}: CodeBlockProps) {
  const block = (
    <pre
      {...preProps}
      className={cx(
        "max-h-64 overflow-auto rounded-md border border-runtime-line-soft/60 bg-runtime-bg p-3 font-mono text-xs leading-relaxed text-ink-soft",
        wrap && "whitespace-pre-wrap break-words",
        className,
      )}
    >
      {children}
    </pre>
  );

  if (!label) return block;

  return (
    <section>
      <div className="mb-2 text-xs uppercase text-ink-muted">{label}</div>
      {block}
    </section>
  );
}

export type CopyableCodeBlockProps = Omit<
  HTMLAttributes<HTMLPreElement>,
  "children"
> & {
  text: string;
  label?: ReactNode;
  copyLabel?: string;
  copiedLabel?: string;
};

export function CopyableCodeBlock({
  text,
  label = "source",
  copyLabel,
  copiedLabel = "copied",
  className,
  ...preProps
}: CopyableCodeBlockProps) {
  const resolvedCopyLabel =
    copyLabel || (typeof label === "string" ? `Copy ${label}` : "Copy source");

  return (
    <div className="overflow-hidden rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70">
      <div className="flex items-center justify-between gap-3 border-b border-runtime-line-soft/60 px-3 py-2 text-xs text-ink-muted">
        <span>{label}</span>
        <CopyButton
          value={text}
          label={resolvedCopyLabel}
          copiedLabel={copiedLabel}
        >
          copy
        </CopyButton>
      </div>
      <pre
        {...preProps}
        className={cx(
          "max-h-[68vh] overflow-auto p-3 font-mono text-xs leading-5 text-ink-soft",
          className,
        )}
      >
        {text}
      </pre>
    </div>
  );
}

export type InfoBlockProps = HTMLAttributes<HTMLDivElement> & {
  title: ReactNode;
};

export function InfoBlock({
  title,
  className,
  children,
  ...divProps
}: InfoBlockProps) {
  return (
    <SurfacePanel
      {...divProps}
      as="div"
      className={cx("bg-runtime-bg/70 p-3", className)}
    >
      <div className="mb-2 text-[10px] uppercase text-ink-faint">
        {title}
      </div>
      {children}
    </SurfacePanel>
  );
}

export type DefinitionRowProps = HTMLAttributes<HTMLDivElement> & {
  label: ReactNode;
  value: ReactNode;
  mono?: boolean;
  valueClassName?: string;
};

export function DefinitionRow({
  label,
  value,
  mono = false,
  className,
  valueClassName,
  ...divProps
}: DefinitionRowProps) {
  return (
    <div
      {...divProps}
      className={cx("grid grid-cols-[72px_1fr] gap-2", className)}
    >
      <dt className="text-ink-faint">{label}</dt>
      <dd
        className={cx(
          "truncate text-ink-soft",
          mono && "font-mono",
          valueClassName,
        )}
      >
        {value}
      </dd>
    </div>
  );
}

type CompactFactTone = "neutral" | "emerald" | "amber" | "red";
type CompactFactSize = "default" | "compact";

const compactFactToneClasses: Record<CompactFactTone, string> = {
  neutral: "text-ink-soft",
  emerald: "text-signal-live",
  amber: "text-signal-authority",
  red: "text-signal-danger",
};

const compactFactSizeClasses: Record<CompactFactSize, string> = {
  default: "mt-1 text-xs",
  compact: "mt-0.5 text-[11px]",
};

export type CompactFactProps = HTMLAttributes<HTMLDivElement> & {
  label: ReactNode;
  value: ReactNode;
  mono?: boolean;
  tone?: CompactFactTone;
  size?: CompactFactSize;
  valueClassName?: string;
};

export function CompactFact({
  label,
  value,
  mono = false,
  tone = "neutral",
  size = "default",
  className,
  valueClassName,
  ...divProps
}: CompactFactProps) {
  return (
    <div {...divProps} className={cx("min-w-0", className)}>
      <dt className="text-[10px] uppercase text-ink-faint">{label}</dt>
      <dd
        className={cx(
          "min-w-0 truncate",
          compactFactSizeClasses[size],
          mono && "font-mono",
          compactFactToneClasses[tone],
          valueClassName,
        )}
      >
        {value}
      </dd>
    </div>
  );
}

export type BadgeFactProps = HTMLAttributes<HTMLDivElement> & {
  label: ReactNode;
  value: ReactNode;
  tone?: StatusBadgeTone;
};

export function BadgeFact({
  label,
  value,
  tone = "neutral",
  className,
  ...divProps
}: BadgeFactProps) {
  return (
    <div {...divProps} className={cx("min-w-0", className)}>
      <dt className="text-[10px] uppercase text-ink-faint">{label}</dt>
      <dd className="mt-1 min-w-0">
        <StatusPill tone={statusBadgeToneToPillTone[tone]} size="xs" dot={false} className="max-w-full rounded-md">
          <span className="truncate">{value}</span>
        </StatusPill>
      </dd>
    </div>
  );
}

export type SelectableSurfaceButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  selected?: boolean;
};

function selectableSurfaceClass(selected: boolean, className?: string) {
  return chromeSurfaceClass(
    selected ? "selected" : "interactive",
    cx(
      "group block w-full px-3 py-3 text-left transition duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/35 focus-visible:ring-offset-2 focus-visible:ring-offset-runtime-bg",
      className,
    ),
  );
}

export function SelectableSurfaceButton({
  selected = false,
  className,
  type = "button",
  children,
  ...buttonProps
}: SelectableSurfaceButtonProps) {
  return (
    <button
      {...buttonProps}
      type={type}
      aria-pressed={buttonProps["aria-pressed"] ?? selected}
      data-selected={selected ? "true" : undefined}
      className={selectableSurfaceClass(selected, className)}
    >
      {children}
    </button>
  );
}

export type SelectableSurfaceLinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  href: string;
  selected?: boolean;
};

export function SelectableSurfaceLink({
  href,
  selected = false,
  className,
  children,
  ...anchorProps
}: SelectableSurfaceLinkProps) {
  const ariaCurrent = anchorProps["aria-current"] ?? (selected ? "page" : undefined);
  const linkClassName = selectableSurfaceClass(selected, className);

  if (href.startsWith("/")) {
    return (
      <Link
        {...anchorProps}
        to={href}
        aria-current={ariaCurrent}
        data-selected={selected ? "true" : undefined}
        className={linkClassName}
      >
        {children}
      </Link>
    );
  }

  return (
    <a
      {...anchorProps}
      href={href}
      aria-current={ariaCurrent}
      data-selected={selected ? "true" : undefined}
      className={linkClassName}
    >
      {children}
    </a>
  );
}

type CommandPaletteItem = {
  id: string;
  label: string;
  group: string;
  path: string;
  description: string;
};

export type CommandPaletteProps = {
  open: boolean;
  query: string;
  items: CommandPaletteItem[];
  activeItemId?: string;
  activePath?: string;
  inputRef?: RefObject<HTMLInputElement>;
  placeholder?: string;
  emptyLabel?: string;
  onQueryChange: (query: string) => void;
  onDismiss: () => void;
  onSubmitItem: (item: CommandPaletteItem) => void;
  onItemClick?: (
    event: MouseEvent<HTMLAnchorElement>,
    item: CommandPaletteItem,
  ) => void;
};

export function CommandPalette({
  open,
  query,
  items,
  activeItemId,
  activePath,
  inputRef,
  placeholder = "Jump to page, tool, or diagnostic",
  emptyLabel = "No matches",
  onQueryChange,
  onDismiss,
  onSubmitItem,
  onItemClick,
}: CommandPaletteProps) {
  const listboxId = useId();
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const boundedHighlightedIndex =
    items.length === 0 ? -1 : Math.min(highlightedIndex, items.length - 1);
  const highlightedItem =
    boundedHighlightedIndex >= 0 ? items[boundedHighlightedIndex] : null;

  useEffect(() => {
    if (open) setHighlightedIndex(0);
  }, [open, query]);

  useEffect(() => {
    setHighlightedIndex((current) =>
      items.length === 0 ? 0 : Math.min(current, items.length - 1),
    );
  }, [items.length]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/55 px-4 py-16 backdrop-blur-sm sm:py-24"
      onMouseDown={onDismiss}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="mx-auto w-full max-w-2xl overflow-hidden rounded-xl border border-runtime-line-soft/80 bg-runtime-bg/95 shadow-2xl shadow-black/70 ring-1 ring-white/5 backdrop-blur-xl"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="border-b border-runtime-line-soft/70 px-4">
          <div className="flex items-center gap-2.5">
            <Icon name="search" size={16} className="shrink-0 text-ink-muted" />
            <input
              ref={inputRef}
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setHighlightedIndex((current) =>
                    items.length === 0 ? 0 : (current + 1) % items.length,
                  );
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setHighlightedIndex((current) =>
                    items.length === 0
                      ? 0
                      : (current - 1 + items.length) % items.length,
                  );
                  return;
                }
                if (event.key === "Enter" && highlightedItem) {
                  event.preventDefault();
                  onSubmitItem(highlightedItem);
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  onDismiss();
                }
              }}
              aria-controls={listboxId}
              aria-activedescendant={
                boundedHighlightedIndex >= 0 ? `${listboxId}-${boundedHighlightedIndex}` : undefined
              }
              placeholder={placeholder}
              className="min-w-0 flex-1 bg-transparent py-3.5 text-sm text-ink outline-none placeholder:text-ink-faint"
            />
            <ToolbarButton
              type="button"
              variant="ghost"
              size="xs"
              onClick={onDismiss}
              className="h-7 w-7 px-0"
              aria-label="Close command palette"
            >
              <Icon name="close" size={14} />
            </ToolbarButton>
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-runtime-line-soft/80 py-2 text-[11px] text-ink-faint">
            <StatusPill tone={items.length > 0 ? "live" : "neutral"} size="xs" dot={false}>
              {items.length} result{items.length === 1 ? "" : "s"}
            </StatusPill>
            <span>Arrow keys move</span>
            <span className="hidden sm:inline">Enter opens</span>
            <span className="hidden sm:inline">Esc closes</span>
          </div>
        </div>
        <div id={listboxId} role="listbox" className="max-h-[60vh] overflow-y-auto p-2">
          {items.length === 0 ? (
            <div className="px-3 py-10 text-center text-sm text-ink-muted">
              <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-lg border border-runtime-line-soft/70 bg-runtime-panel/60">
                <Icon name="search" size={18} />
              </div>
              <div className="font-medium text-ink-soft">{emptyLabel}</div>
              <div className="mt-1 text-xs text-ink-faint">
                Try a page, tool, route, or diagnostic keyword.
              </div>
            </div>
          ) : (
            items.map((item, index) => {
              const active = activeItemId === item.id && activePath === item.path;
              const highlighted = index === boundedHighlightedIndex;
              const [scope, leafLabel] = item.label.includes(" / ")
                ? item.label.split(" / ", 2)
                : ["", item.label];
              return (
                <SelectableSurfaceLink
                  key={`${item.id}:${item.path}`}
                  id={`${listboxId}-${index}`}
                  role="option"
                  aria-selected={highlighted}
                  href={item.path}
                  onClick={(event) => onItemClick?.(event, item)}
                  onMouseEnter={() => setHighlightedIndex(index)}
                  selected={active}
                  className={cx(
                    "flex items-center justify-between gap-4 px-3 py-2.5",
                    highlighted
                      && !active
                      && "border-signal-protocol/30 bg-runtime-panel/75 ring-1 ring-signal-protocol/10",
                  )}
                >
                  <span className="flex min-w-0 items-start gap-3">
                    <span
                      className={cx(
                        "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border",
                        active
                          ? "border-signal-protocol/40 bg-signal-protocol/15 text-signal-protocol"
                          : highlighted
                            ? "border-runtime-line bg-runtime-panel text-ink-soft"
                            : "border-runtime-line-soft/70 bg-runtime-bg text-ink-muted",
                      )}
                      aria-hidden="true"
                    >
                      <Icon name={active ? "check" : highlighted ? "arrow-right" : "file"} size={14} />
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium">
                        {scope && (
                          <>
                            <span className="font-normal text-ink-muted">{scope}</span>
                            <span className="mx-1.5 text-ink-faint">/</span>
                          </>
                        )}
                        {leafLabel}
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-ink-muted">
                        {item.group} - {item.description}
                      </span>
                    </span>
                  </span>
                  <span className="hidden min-w-[8rem] shrink-0 text-right sm:block">
                    {active ? (
                      <StatusPill tone="live" size="xs" dot={false}>current</StatusPill>
                    ) : (
                      <span className="font-mono text-xs text-ink-faint">
                        {item.path}
                      </span>
                    )}
                  </span>
                </SelectableSurfaceLink>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  size = "default",
  className,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  size?: "default" | "compact";
  className?: string;
}) {
  return (
    <div
      className={chromeSurfaceClass(
        "empty",
        cx(
          "text-center",
          size === "compact" ? "px-3 py-6" : "px-5 py-12 sm:px-6 sm:py-14",
          className,
        ),
      )}
    >
      <div className="mx-auto mb-3 h-px w-12 bg-runtime-line/80" aria-hidden="true" />
      <div className="text-sm font-semibold text-ink-soft">{title}</div>
      {description && (
        <div
          className={cx(
            "mx-auto mt-2 max-w-md leading-relaxed text-ink-muted",
            size === "compact" ? "text-xs" : "text-sm",
          )}
        >
          {description}
        </div>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function LoadingState({ label = "Loading..." }: { label?: string }) {
  return (
    <div
      className={chromeSurfaceClass(
        "panel",
        "flex items-center gap-3 px-4 py-5 text-sm text-ink-muted",
      )}
    >
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-runtime-line border-t-signal-protocol" aria-hidden="true" />
      <span className="min-w-0 truncate">{label}</span>
    </div>
  );
}

export function InlineAlert({
  tone,
  className,
  children,
  ...divProps
}: {
  tone: "red" | "emerald" | "amber" | "neutral";
  children: ReactNode;
} & HTMLAttributes<HTMLDivElement>) {
  const cls =
    tone === "red"
      ? "border-signal-danger/50 bg-signal-danger/12 text-signal-danger"
      : tone === "emerald"
        ? "border-signal-live/45 bg-signal-live/12 text-signal-live"
        : tone === "amber"
          ? "border-signal-authority/45 bg-signal-authority/12 text-signal-authority"
        : "border-runtime-line-soft/60 bg-runtime-panel/70 text-ink-soft";
  return (
    <div
      {...divProps}
      className={cx("rounded-lg border px-3.5 py-2.5 text-sm", cls, className)}
    >
      {children}
    </div>
  );
}

export function SummaryStrip({
  children,
  className,
  ...divProps
}: {
  children: ReactNode;
  className?: string;
} & HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      {...divProps}
      className={cx(
        "grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function SummaryMetric({
  label,
  value,
  detail,
  tone = "neutral",
  size = "default",
  mono,
  className,
  children,
  ...divProps
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: "neutral" | "emerald" | "amber" | "red";
  size?: "default" | "compact";
  mono?: boolean;
  children?: ReactNode;
} & HTMLAttributes<HTMLDivElement>) {
  const toneClass =
    tone === "emerald"
      ? "text-signal-live"
      : tone === "amber"
        ? "text-signal-authority"
        : tone === "red"
          ? "text-signal-danger"
          : "text-ink";
  const compact = size === "compact";
  const monoValue = mono ?? compact;
  return (
    <div
      {...divProps}
      className={chromeSurfaceClass(
        "solid",
        cx("min-w-0", compact ? "px-2.5 py-2" : "px-3.5 py-3", className),
      )}
    >
      <div className={cx(compact ? "text-[10px]" : "text-[11px]", "font-medium uppercase tracking-wide text-ink-faint")}>
        {label}
      </div>
      <div
        className={cx(
          "mt-1 truncate font-semibold",
          compact ? "text-[11px]" : "text-lg",
          monoValue && "font-mono",
          toneClass,
        )}
      >
        {value}
      </div>
      {detail && <div className="mt-1 truncate text-xs text-ink-faint">{detail}</div>}
      {children}
    </div>
  );
}

export function FilterBar({
  children,
  actions,
}: {
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div
      className={chromeSurfaceClass(
        "solid",
        "flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-end sm:justify-between",
      )}
    >
      <div className="flex min-w-0 flex-wrap items-end gap-3">{children}</div>
      {actions && <div className="shrink-0">{actions}</div>}
    </div>
  );
}

export function DataTable({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={chromeSurfaceClass("table", cx("overflow-hidden", className))}>
      <div className="overflow-x-auto">{children}</div>
    </div>
  );
}
