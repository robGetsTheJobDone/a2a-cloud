import {
  useId,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { uploadFile, type FileMeta } from "../api";
import {
  InlineAlert,
  ToolbarButton,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";
import { Icon } from "./Icon";

const MAX_TEXTAREA_HEIGHT = 220;

export type ChatAttachment = {
  uri: string;
  path: string;
  name: string;
  mime_type: string;
  size_bytes: number;
};

export type ChatComposerProps = {
  busy?: boolean;
  disabled?: boolean;
  placeholder?: string;
  value?: string;
  onValueChange?: (value: string) => void;
  onCancel: () => void;
  onSend: (
    text: string,
    attachments: ChatAttachment[],
  ) => Promise<void> | void;
};

type ComposerFile = {
  id: string;
  file: File;
  name: string;
  size: number;
  status: "uploading" | "ready" | "error";
  error?: string;
  attachment?: ChatAttachment;
};

function uploadPrefix() {
  const stamp = new Date().toISOString().replace(/[^0-9A-Za-z]+/g, "-");
  const suffix = Math.random().toString(36).slice(2, 8);
  return `inputs/chat/${stamp}-${suffix}`;
}

function fileId(file: File) {
  const suffix = Math.random().toString(36).slice(2, 8);
  return `${file.name}-${file.size}-${file.lastModified}-${suffix}`;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function toAttachment(file: File, meta: FileMeta): ChatAttachment {
  return {
    uri: `workspace://${meta.path}`,
    path: meta.path,
    name: file.name,
    mime_type: meta.content_type || file.type || "application/octet-stream",
    size_bytes: meta.size,
  };
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function hasDraggedFiles(event: DragEvent<HTMLElement>) {
  return Array.from(event.dataTransfer.types).includes("Files");
}

export function ChatComposer({
  busy = false,
  disabled = false,
  placeholder = "Ask the main agent…",
  value,
  onValueChange,
  onCancel,
  onSend,
}: ChatComposerProps) {
  const [internalText, setInternalText] = useState("");
  const [files, setFiles] = useState<ComposerFile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dragDepth, setDragDepth] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const statusId = useId();
  const errorId = useId();
  const controlled = value !== undefined;
  const text = controlled ? value : internalText;

  function setText(next: string) {
    if (!controlled) setInternalText(next);
    onValueChange?.(next);
  }

  const readyAttachments = useMemo(
    () =>
      files.flatMap((item) =>
        item.status === "ready" && item.attachment ? [item.attachment] : [],
      ),
    [files],
  );
  const uploadingCount = files.filter((item) => item.status === "uploading").length;
  const failedCount = files.filter((item) => item.status === "error").length;
  const isDisabled = disabled || submitting;
  const canEdit = !isDisabled && !busy;
  const canSend =
    canEdit &&
    uploadingCount === 0 &&
    failedCount === 0 &&
    (text.trim().length > 0 || readyAttachments.length > 0);
  const isDragging = dragDepth > 0 && canEdit;
  const describedBy = error ? `${statusId} ${errorId}` : statusId;
  const statusText = busy
    ? "Response in progress."
    : uploadingCount > 0
      ? `Uploading ${uploadingCount} file${uploadingCount === 1 ? "" : "s"}.`
      : failedCount > 0
        ? `Resolve ${failedCount} failed upload${failedCount === 1 ? "" : "s"} before sending.`
        : readyAttachments.length > 0
          ? `${readyAttachments.length} attachment${readyAttachments.length === 1 ? "" : "s"} ready.`
          : "Composer ready.";
  const showVisibleStatus = busy || uploadingCount > 0 || failedCount > 0;

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    const nextHeight = Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY =
      textarea.scrollHeight > MAX_TEXTAREA_HEIGHT ? "auto" : "hidden";
  }, [text]);

  function updateFile(id: string, patch: Partial<ComposerFile>) {
    setFiles((current) =>
      current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  }

  async function uploadOne(item: ComposerFile, prefix: string) {
    try {
      const meta = await uploadFile(item.file, prefix);
      updateFile(item.id, {
        status: "ready",
        error: undefined,
        attachment: toAttachment(item.file, meta),
      });
    } catch (caught) {
      updateFile(item.id, {
        status: "error",
        error: errorMessage(caught),
      });
      setError("One or more files failed to upload.");
    }
  }

  function addFiles(nextFiles: FileList | File[]) {
    const picked = Array.from(nextFiles).filter((file) => file.name);
    if (!picked.length || !canEdit) return;
    setError(null);
    const prefix = uploadPrefix();
    const queued = picked.map((file) => ({
      id: fileId(file),
      file,
      name: file.name,
      size: file.size,
      status: "uploading" as const,
    }));
    setFiles((current) => [...current, ...queued]);
    void Promise.all(queued.map((item) => uploadOne(item, prefix)));
  }

  function retryUpload(item: ComposerFile) {
    if (!canEdit) return;
    setError(null);
    updateFile(item.id, {
      status: "uploading",
      error: undefined,
      attachment: undefined,
    });
    void uploadOne(item, uploadPrefix());
  }

  function removeFile(id: string) {
    setFiles((current) => current.filter((item) => item.id !== id));
  }

  function resetFileInput() {
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function submit() {
    if (!canSend) return;
    const sentText = text.trim();
    const sentFiles = files;
    const sentAttachments = readyAttachments;
    setText("");
    setFiles([]);
    setError(null);
    resetFileInput();
    setSubmitting(true);
    try {
      await onSend(sentText, sentAttachments);
    } catch (caught) {
      setText(sentText);
      setFiles(sentFiles);
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.key !== "Enter") return;
    if (event.shiftKey && !event.metaKey && !event.ctrlKey) return;
    event.preventDefault();
    void submit();
  }

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const pastedFiles = event.clipboardData.files;
    if (!pastedFiles.length) return;
    event.preventDefault();
    addFiles(pastedFiles);
  }

  function onDragEnter(event: DragEvent<HTMLElement>) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    setDragDepth((depth) => depth + 1);
  }

  function onDragOver(event: DragEvent<HTMLElement>) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = canEdit ? "copy" : "none";
  }

  function onDragLeave(event: DragEvent<HTMLElement>) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    setDragDepth((depth) => Math.max(0, depth - 1));
  }

  function onDrop(event: DragEvent<HTMLElement>) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    setDragDepth(0);
    addFiles(event.dataTransfer.files);
  }

  return (
    <form
      aria-busy={busy || submitting || uploadingCount > 0}
      aria-label="Chat composer"
      className="shrink-0 border-t border-runtime-line-soft/60 bg-runtime-bg px-4 pb-4 pt-3"
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={onDragOver}
      onDrop={onDrop}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div
        className={`relative mx-auto max-w-3xl overflow-hidden rounded-2xl border bg-runtime-bg shadow-[0_1px_0_rgba(255,255,255,0.04)_inset] transition-colors ${
          isDragging
            ? "border-signal-authority/45 ring-2 ring-signal-authority/15"
            : "border-runtime-line-soft/60 focus-within:border-runtime-line-mid"
        }`}
      >
        {isDragging && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-runtime-bg/95 text-xs font-medium text-signal-authority">
            <Icon name="paperclip" size={14} className="mr-2" />
            Drop files to attach
          </div>
        )}

        {files.length > 0 && (
          <ul
            aria-label="Attached files"
            className="flex flex-wrap gap-2 border-b border-runtime-line-soft/60 px-3 py-2"
          >
            {files.map((item) => (
              <li
                key={item.id}
                className={`group flex max-w-full items-center gap-2 rounded-full border px-2.5 py-1 text-[11px] ${
                  item.status === "error"
                    ? "border-signal-danger/50 bg-signal-danger/12 text-signal-danger"
                    : item.status === "uploading"
                      ? "border-signal-authority/45 bg-signal-authority/12 text-signal-authority"
                      : "border-runtime-line-soft/60 bg-runtime-bg text-ink-soft"
                }`}
              >
                <Icon
                  name={item.status === "error" ? "warning" : "file"}
                  size={12}
                  className={
                    item.status === "uploading"
                      ? "animate-pulse text-signal-authority"
                      : item.status === "error"
                        ? "text-signal-danger"
                        : "text-ink-muted"
                  }
                />
                <span className="min-w-0 max-w-[14rem] truncate font-medium">
                  {item.name}
                </span>
                <span className="shrink-0 text-ink-muted">
                  {formatBytes(item.size)}
                </span>
                {item.status === "uploading" && (
                  <StateBadge status="uploading" size="xs" />
                )}
                {item.status === "error" && (
                  <>
                    <span className="min-w-0 max-w-[12rem] truncate text-signal-danger">
                      {item.error || "upload failed"}
                    </span>
                    <ToolbarButton
                      disabled={!canEdit}
                      onClick={() => retryUpload(item)}
                      variant="danger"
                      size="xs"
                      className="h-5 rounded-full px-2 text-[10px] uppercase"
                    >
                      retry
                    </ToolbarButton>
                  </>
                )}
                <ToolbarButton
                  aria-label={`Remove ${item.name}`}
                  disabled={!canEdit}
                  onClick={() => removeFile(item.id)}
                  variant="ghost"
                  size="xs"
                  className="ml-0.5 h-5 w-5 rounded-full px-0"
                >
                  <Icon name="x" size={10} />
                </ToolbarButton>
              </li>
            ))}
          </ul>
        )}

        <div className="flex items-end gap-2 px-3 py-2">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            aria-label="Upload files"
            disabled={!canEdit}
            onChange={(event) => {
              addFiles(event.target.files || []);
              resetFileInput();
            }}
            className="sr-only"
          />
          <ToolbarButton
            aria-label="Attach files"
            title="Attach files"
            disabled={!canEdit}
            onClick={() => fileInputRef.current?.click()}
            variant="ghost"
            size="md"
            className="h-9 w-9 px-0"
          >
            <Icon name="paperclip" size={16} />
          </ToolbarButton>

          <textarea
            ref={textareaRef}
            rows={1}
            value={text}
            disabled={!canEdit}
            aria-describedby={describedBy}
            aria-label="Message main agent"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            placeholder={placeholder}
            className="min-h-9 flex-1 resize-none bg-transparent py-2 text-[14px] leading-6 text-ink outline-none placeholder:text-ink-faint disabled:cursor-not-allowed disabled:opacity-60"
          />

          {busy ? (
            <ToolbarButton
              aria-label="Stop response"
              title="Stop response"
              onClick={onCancel}
              variant="danger"
              size="md"
              className="h-9 w-9 px-0"
            >
              <Icon name="stop" size={12} strokeWidth={2} />
            </ToolbarButton>
          ) : (
            <ToolbarButton
              type="submit"
              aria-label="Send message"
              title="Send · Enter"
              disabled={!canSend}
              variant="primary"
              size="md"
              className="h-9 w-9 px-0"
            >
              <Icon name="send" size={14} strokeWidth={2} />
            </ToolbarButton>
          )}
        </div>
      </div>

      <div className="mx-auto mt-2 flex max-w-3xl items-center justify-between text-[11px] text-ink-faint">
        <div
          id={statusId}
          role="status"
          aria-live="polite"
          className={showVisibleStatus ? "text-ink-muted" : "sr-only"}
        >
          {statusText}
        </div>
        <div className="hidden font-mono text-[10px] text-ink-faint md:flex md:items-center md:gap-3">
          <span><kbd className="rounded-md border border-runtime-line-soft/60 px-1 text-ink-muted">Enter</kbd> send</span>
          <span><kbd className="rounded-md border border-runtime-line-soft/60 px-1 text-ink-muted">⇧↵</kbd> newline</span>
          <span><kbd className="rounded-md border border-runtime-line-soft/60 px-1 text-ink-muted">⌘V</kbd> paste files</span>
        </div>
      </div>
      {error && (
        <InlineAlert
          id={errorId}
          role="alert"
          tone="red"
          className="mx-auto mt-2 max-w-3xl text-xs"
        >
          {error}
        </InlineAlert>
      )}
    </form>
  );
}
