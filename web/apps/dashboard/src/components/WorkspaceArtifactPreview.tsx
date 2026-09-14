import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";
import { fetchFile, type SubagentFileOp } from "../api";
import { workspaceArtifactHref } from "../navigation";
import {
  CopyableCodeBlock,
  EmptyState,
  InlineAlert,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";

const MarkdownBody = lazy(() => import("./ChatMarkdownBody"));

const TEXT_PREVIEW_LIMIT = 1024 * 1024;
const TABLE_PREVIEW_ROWS = 500;
const TEXT_SNAPSHOT_CACHE_LIMIT = 30;
const DIFF_CONTEXT_LINES = 3;

const CODE_SUFFIXES = new Set([
  "bash",
  "c",
  "cc",
  "conf",
  "cpp",
  "cs",
  "css",
  "env",
  "fish",
  "go",
  "h",
  "hpp",
  "ini",
  "java",
  "js",
  "jsx",
  "php",
  "py",
  "rb",
  "rs",
  "scss",
  "sh",
  "sql",
  "toml",
  "ts",
  "tsx",
  "xml",
  "yaml",
  "yml",
  "zsh",
]);

const TEXT_SUFFIXES = new Set([
  "gitignore",
  "log",
  "markdown",
  "md",
  "rst",
  "text",
  "txt",
]);

export type WorkspaceArtifactKind =
  | "image"
  | "svg"
  | "audio"
  | "video"
  | "pdf"
  | "csv"
  | "tsv"
  | "json"
  | "html"
  | "text"
  | "code"
  | "binary";

export type WorkspaceArtifact = {
  path: string;
  size?: number | null;
  content_type?: string | null;
  modified_at?: string | null;
  etag?: string | null;
  op?: SubagentFileOp["op"] | string | null;
};

export type WorkspaceArtifactFetcher = (path: string) => Promise<Blob>;

type WorkspaceArtifactVariant = "card" | "list";

type WorkspaceArtifactAction = (
  artifact: WorkspaceArtifact,
) => void | Promise<void>;

export type WorkspaceArtifactListProps = {
  artifacts: readonly WorkspaceArtifact[];
  variant?: WorkspaceArtifactVariant;
  className?: string;
  emptyLabel?: ReactNode;
  maxItems?: number;
  showDeleted?: boolean;
  fetcher?: WorkspaceArtifactFetcher;
  onOpen?: WorkspaceArtifactAction;
  onDownload?: WorkspaceArtifactAction;
  hrefForArtifact?: (artifact: WorkspaceArtifact) => string;
};

type WorkspaceArtifactCardProps = {
  artifact: WorkspaceArtifact;
  variant?: WorkspaceArtifactVariant;
  className?: string;
  downloading?: boolean;
  disabled?: boolean;
  href?: string;
  onOpen?: WorkspaceArtifactAction;
  onDownload?: WorkspaceArtifactAction;
};

export type WorkspaceArtifactPreviewPanelProps = {
  artifact: WorkspaceArtifact;
  fetcher?: WorkspaceArtifactFetcher;
  actions?: ReactNode;
  className?: string;
};

type PreviewState = {
  blob: Blob | null;
  url: string | null;
  text: string | null;
  error: string | null;
};

type TextDiffLine = {
  kind: "context" | "add" | "remove" | "skip";
  text: string;
  beforeLine?: number;
  afterLine?: number;
};

export type TextDiff = {
  lines: TextDiffLine[];
  added: number;
  removed: number;
};

type TextSnapshot = {
  revision: string;
  text: string;
};

const textSnapshotCache = new Map<string, TextSnapshot>();

export function buildTextDiff(before: string, after: string): TextDiff | null {
  if (before === after) return null;
  const beforeLines = before.split(/\r?\n/);
  const afterLines = after.split(/\r?\n/);
  let prefix = 0;
  while (
    prefix < beforeLines.length &&
    prefix < afterLines.length &&
    beforeLines[prefix] === afterLines[prefix]
  ) {
    prefix += 1;
  }

  let suffix = 0;
  while (
    suffix < beforeLines.length - prefix &&
    suffix < afterLines.length - prefix &&
    beforeLines[beforeLines.length - 1 - suffix] ===
      afterLines[afterLines.length - 1 - suffix]
  ) {
    suffix += 1;
  }

  const lines: TextDiffLine[] = [];
  const contextStart = Math.max(0, prefix - DIFF_CONTEXT_LINES);
  if (contextStart > 0) {
    lines.push({
      kind: "skip",
      text: `${contextStart} unchanged ${contextStart === 1 ? "line" : "lines"}`,
    });
  }
  for (let index = contextStart; index < prefix; index += 1) {
    lines.push({
      kind: "context",
      text: beforeLines[index],
      beforeLine: index + 1,
      afterLine: index + 1,
    });
  }

  const beforeChangeEnd = beforeLines.length - suffix;
  const afterChangeEnd = afterLines.length - suffix;
  for (let index = prefix; index < beforeChangeEnd; index += 1) {
    lines.push({
      kind: "remove",
      text: beforeLines[index],
      beforeLine: index + 1,
    });
  }
  for (let index = prefix; index < afterChangeEnd; index += 1) {
    lines.push({
      kind: "add",
      text: afterLines[index],
      afterLine: index + 1,
    });
  }

  const suffixContext = Math.min(suffix, DIFF_CONTEXT_LINES);
  for (let offset = 0; offset < suffixContext; offset += 1) {
    const beforeIndex = beforeChangeEnd + offset;
    const afterIndex = afterChangeEnd + offset;
    lines.push({
      kind: "context",
      text: beforeLines[beforeIndex],
      beforeLine: beforeIndex + 1,
      afterLine: afterIndex + 1,
    });
  }
  if (suffix > suffixContext) {
    const hidden = suffix - suffixContext;
    lines.push({
      kind: "skip",
      text: `${hidden} unchanged ${hidden === 1 ? "line" : "lines"}`,
    });
  }

  return {
    lines,
    added: Math.max(0, afterChangeEnd - prefix),
    removed: Math.max(0, beforeChangeEnd - prefix),
  };
}

function rememberTextSnapshot(path: string, snapshot: TextSnapshot) {
  textSnapshotCache.delete(path);
  textSnapshotCache.set(path, snapshot);
  while (textSnapshotCache.size > TEXT_SNAPSHOT_CACHE_LIMIT) {
    const oldest = textSnapshotCache.keys().next().value as string | undefined;
    if (!oldest) break;
    textSnapshotCache.delete(oldest);
  }
}

function artifactRevision(artifact: WorkspaceArtifact, blob: Blob) {
  return (
    artifact.etag ||
    artifact.modified_at ||
    `${artifact.path}:${blob.size}:${blob.type || artifact.content_type || "unknown"}`
  );
}

export function WorkspaceArtifactList({
  artifacts,
  variant = "card",
  className,
  emptyLabel = "No workspace files.",
  maxItems,
  showDeleted = false,
  fetcher = fetchFile,
  onOpen,
  onDownload,
  hrefForArtifact,
}: WorkspaceArtifactListProps) {
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const { visibleArtifacts, hiddenArtifactCount } = useMemo(() => {
    const filtered = showDeleted
      ? artifacts
      : artifacts.filter((artifact) => artifact.op !== "delete");
    if (typeof maxItems !== "number") {
      return { visibleArtifacts: filtered, hiddenArtifactCount: 0 };
    }

    const cappedMax = Math.max(0, maxItems);
    return {
      visibleArtifacts: filtered.slice(0, cappedMax),
      hiddenArtifactCount: Math.max(0, filtered.length - cappedMax),
    };
  }, [artifacts, maxItems, showDeleted]);

  const handleOpen = useCallback(
    async (artifact: WorkspaceArtifact) => {
      setActionError(null);
      if (!onOpen) {
        return;
      }
      try {
        await onOpen(artifact);
      } catch (ex) {
        setActionError(messageFromError(ex));
      }
    },
    [onOpen],
  );

  const handleDownload = useCallback(
    async (artifact: WorkspaceArtifact) => {
      setActionError(null);
      setDownloadingPath(artifact.path);
      try {
        if (onDownload) {
          await onDownload(artifact);
        } else {
          await downloadWorkspaceArtifact(artifact, fetcher);
        }
      } catch (ex) {
        setActionError(messageFromError(ex));
      } finally {
        setDownloadingPath(null);
      }
    },
    [fetcher, onDownload],
  );

  if (!visibleArtifacts.length) {
    return typeof emptyLabel === "string" ? (
      <EmptyState title={emptyLabel} size="compact" className={className} />
    ) : (
      <SurfacePanel
        as="div"
        className={cx(
          "bg-runtime-bg px-3 py-4 text-center text-sm text-ink-muted",
          className,
        )}
      >
        {emptyLabel}
      </SurfacePanel>
    );
  }

  return (
    <div className={cx("min-w-0", className)}>
      <div
        className={
          variant === "card"
            ? "grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-3"
            : "space-y-1"
        }
      >
        {visibleArtifacts.map((artifact, index) => (
          <WorkspaceArtifactCard
            key={`${artifact.path}-${artifact.op ?? "file"}-${index}`}
            artifact={artifact}
            variant={variant}
            downloading={downloadingPath === artifact.path}
            href={hrefForArtifact?.(artifact) ?? workspaceArtifactHref(artifact.path)}
            onOpen={handleOpen}
            onDownload={handleDownload}
          />
        ))}
      </div>
      {hiddenArtifactCount > 0 && (
        <div className="mt-2 text-xs text-ink-muted">
          + {hiddenArtifactCount} more files
        </div>
      )}
      {actionError && (
        <InlineAlert
          tone="red"
          role="alert"
          className="mt-2 [overflow-wrap:anywhere]"
        >
          {actionError}
        </InlineAlert>
      )}
    </div>
  );
}

function WorkspaceArtifactCard({
  artifact,
  variant = "card",
  className,
  downloading = false,
  disabled = false,
  href,
  onOpen,
  onDownload,
}: WorkspaceArtifactCardProps) {
  const kind = workspaceArtifactKind(artifact);
  const name = workspaceArtifactName(artifact.path);
  const deleted = artifact.op === "delete";
  const canOpen = Boolean(onOpen || href) && !deleted && !disabled;
  const canDownload = Boolean(onDownload) && !deleted && !disabled;

  const title = (
    <>
      <span className="block truncate font-mono text-xs text-ink">
        {name}
      </span>
      <span className="mt-1 block min-w-0 truncate text-[11px] text-ink-muted">
        {artifact.path}
      </span>
      <ArtifactMeta artifact={artifact} />
    </>
  );

  if (variant === "list") {
    return (
      <SurfacePanel
        as="div"
        className={cx(
          "grid min-w-0 grid-cols-[2.75rem_minmax(0,1fr)] items-center gap-x-2 gap-y-2 bg-runtime-bg/70 px-2 py-2 text-xs hover:border-runtime-line-mid sm:grid-cols-[2.75rem_minmax(0,1fr)_auto_auto]",
          deleted && "opacity-60",
          className,
        )}
      >
        <ArtifactBadge kind={kind} path={artifact.path} />
        {href && canOpen ? (
          <Link
            to={href}
            title={artifact.path}
            className="min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/50"
          >
            {title}
          </Link>
        ) : (
          <button
            type="button"
            onClick={() => onOpen?.(artifact)}
            disabled={!canOpen}
            title={artifact.path}
            className="min-w-0 text-left disabled:cursor-default disabled:opacity-70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/50"
          >
            {title}
          </button>
        )}
        {href && canOpen ? (
          <ToolbarLink
            href={href}
            size="xs"
            className="col-start-2 justify-self-start sm:col-start-auto"
            aria-label={`Open preview for ${artifact.path}`}
          >
            Open
          </ToolbarLink>
        ) : (
          <ToolbarButton
            onClick={() => onOpen?.(artifact)}
            disabled={!canOpen}
            size="xs"
            className="col-start-2 justify-self-start disabled:cursor-default sm:col-start-auto"
            aria-label={`Open preview for ${artifact.path}`}
          >
            Open
          </ToolbarButton>
        )}
        <ToolbarButton
          onClick={() => onDownload?.(artifact)}
          disabled={!canDownload || downloading}
          size="xs"
          className="col-start-2 justify-self-start disabled:cursor-wait sm:col-start-auto"
          aria-label={`Download ${artifact.path}`}
        >
          {downloading ? "Downloading..." : "Download"}
        </ToolbarButton>
      </SurfacePanel>
    );
  }

  return (
    <SurfacePanel
      as="article"
      className={cx(
        "min-w-0 bg-runtime-panel/60 p-3",
        deleted && "opacity-60",
        className,
      )}
    >
      {href && canOpen ? (
        <Link
          to={href}
          className="flex w-full min-w-0 items-start gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/50"
        >
          <ArtifactBadge kind={kind} path={artifact.path} />
          <span className="min-w-0 flex-1">{title}</span>
        </Link>
      ) : (
        <button
          type="button"
          onClick={() => onOpen?.(artifact)}
          disabled={!canOpen}
          className="flex w-full min-w-0 items-start gap-3 text-left disabled:cursor-default disabled:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/50"
        >
          <ArtifactBadge kind={kind} path={artifact.path} />
          <span className="min-w-0 flex-1">{title}</span>
        </button>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {href && canOpen ? (
          <ToolbarLink
            href={href}
            size="xs"
            aria-label={`Open preview for ${artifact.path}`}
          >
            Open
          </ToolbarLink>
        ) : (
          <ToolbarButton
            onClick={() => onOpen?.(artifact)}
            disabled={!canOpen}
            size="xs"
            className="disabled:cursor-default"
            aria-label={`Open preview for ${artifact.path}`}
          >
            Open
          </ToolbarButton>
        )}
        <ToolbarButton
          onClick={() => onDownload?.(artifact)}
          disabled={!canDownload || downloading}
          size="xs"
          className="disabled:cursor-wait"
          aria-label={`Download ${artifact.path}`}
        >
          {downloading ? "Downloading..." : "Download"}
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}

export function WorkspaceArtifactPreviewPanel({
  artifact,
  fetcher = fetchFile,
  actions,
  className,
}: WorkspaceArtifactPreviewPanelProps) {
  const [{ blob, url, text, error }, setPreviewState] = useState<PreviewState>({
    blob: null,
    url: null,
    text: null,
    error: null,
  });
  const [textDiff, setTextDiff] = useState<TextDiff | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const titleId = useId();
  const metaId = useId();
  const kind = workspaceArtifactKind(artifact, blob ?? undefined);
  const name = workspaceArtifactName(artifact.path);

  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;

    setPreviewState({ blob: null, url: null, text: null, error: null });
    setTextDiff(null);
    setShowDiff(false);
    fetcher(artifact.path)
      .then(async (rawBlob) => {
        if (!alive) return;
        const nextBlob = typedArtifactBlob(rawBlob, artifact);
        objectUrl = URL.createObjectURL(nextBlob);
        setPreviewState({
          blob: nextBlob,
          url: objectUrl,
          text: null,
          error: null,
        });

        const nextKind = workspaceArtifactKind(artifact, nextBlob);
        if (!isTextPreviewKind(nextKind) || nextBlob.size > TEXT_PREVIEW_LIMIT) {
          return;
        }

        const body = await nextBlob.text();
        if (alive) {
          const previous = textSnapshotCache.get(artifact.path);
          const revision = artifactRevision(artifact, nextBlob);
          const nextDiff =
            previous && previous.revision !== revision
              ? buildTextDiff(previous.text, body)
              : null;
          rememberTextSnapshot(artifact.path, {
            revision,
            text: body,
          });
          setTextDiff(nextDiff);
          setShowDiff(Boolean(nextDiff));
          setPreviewState({
            blob: nextBlob,
            url: objectUrl,
            text: body,
            error: null,
          });
        }
      })
      .catch((ex) => {
        if (alive) {
          setTextDiff(null);
          setShowDiff(false);
          setPreviewState({
            blob: null,
            url: null,
            text: null,
            error: messageFromError(ex),
          });
        }
      });

    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  // Callers commonly create the artifact descriptor inline from the current
  // route. Depending on the object itself would restart this request after
  // every preview-state render, leaving a clicked file stuck in a fetch loop.
  // Only the fields that determine the fetched/typed payload should reload it.
  }, [artifact.path, artifact.content_type, artifact.etag, artifact.modified_at, fetcher]);

  return (
    <SurfacePanel
      as="section"
      className={cx(
        "flex min-h-[32rem] min-w-0 flex-col overflow-hidden bg-runtime-bg",
        className,
      )}
    >
      <header className="flex flex-col gap-3 border-b border-runtime-line-soft/60 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h2 id={titleId} className="truncate font-mono text-sm text-ink">
            {name}
          </h2>
          <div
            id={metaId}
            className="mt-1 flex min-w-0 flex-wrap items-center gap-2 text-[11px] text-ink-muted"
          >
            <span className="rounded-md border border-runtime-line-soft/60 px-1.5 py-0.5 font-mono text-ink-soft">
              {workspaceArtifactLabel(kind, artifact.path)}
            </span>
            <span className="min-w-0 truncate">{artifact.path}</span>
            <span>{formatArtifactSize(artifact.size)}</span>
            {artifact.content_type && (
              <span className="min-w-0 truncate">{artifact.content_type}</span>
            )}
            {artifact.modified_at && <span>{formatArtifactDate(artifact.modified_at)}</span>}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          {actions}
          {textDiff && (
            <ToolbarButton
              onClick={() => setShowDiff((current) => !current)}
              variant={showDiff ? "primary" : "ghost"}
              aria-pressed={showDiff}
              aria-label={`${showDiff ? "Hide" : "Show"} changes for ${name}`}
            >
              {showDiff
                ? "Show current"
                : `View diff +${textDiff.added} −${textDiff.removed}`}
            </ToolbarButton>
          )}
          {url && (
            <ToolbarLink
              href={url}
              external
              aria-label={`Open ${name} in a new tab`}
            >
              Open raw
            </ToolbarLink>
          )}
          {url && (
            <ToolbarLink
              href={url}
              download={name}
              aria-label={`Download ${name}`}
            >
              Download
            </ToolbarLink>
          )}
        </div>
      </header>

      <div
        aria-labelledby={titleId}
        aria-describedby={metaId}
        className="min-h-0 flex-1 overflow-auto bg-runtime-bg p-4 sm:min-h-[360px]"
      >
        {error ? (
          <InlineAlert
            tone="red"
            role="alert"
            className="[overflow-wrap:anywhere]"
          >
            {error}
          </InlineAlert>
        ) : !blob || !url ? (
          <div className="py-16 text-center text-sm text-ink-muted" role="status">
            Loading...
          </div>
        ) : showDiff && textDiff ? (
          <TextDiffPanel diff={textDiff} path={artifact.path} />
        ) : (
          <ArtifactPreviewBody
            artifact={artifact}
            kind={kind}
            text={text}
            url={url}
          />
        )}
      </div>
    </SurfacePanel>
  );
}

export async function downloadWorkspaceArtifact(
  artifact: WorkspaceArtifact,
  fetcher: WorkspaceArtifactFetcher = fetchFile,
) {
  const blob = typedArtifactBlob(await fetcher(artifact.path), artifact);
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = workspaceArtifactName(artifact.path);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function workspaceArtifactKind(
  artifact: WorkspaceArtifact,
  blob?: Blob,
): WorkspaceArtifactKind {
  const type = (blob?.type || artifact.content_type || "").toLowerCase();
  const suffix = workspaceArtifactExt(artifact.path);
  const base = workspaceArtifactName(artifact.path).toLowerCase();

  if (type.startsWith("image/")) return type === "image/svg+xml" ? "svg" : "image";
  if (suffix === "svg") return "svg";
  if (type.startsWith("audio/")) return "audio";
  if (type.startsWith("video/")) return "video";
  if (type === "application/pdf" || suffix === "pdf") return "pdf";
  if (suffix === "csv" || type.includes("csv")) return "csv";
  if (suffix === "tsv" || type.includes("tab-separated-values")) return "tsv";
  if (suffix === "json" || type.includes("json")) return "json";
  if (suffix === "html" || suffix === "htm" || type.includes("html")) return "html";
  if (
    CODE_SUFFIXES.has(suffix) ||
    base === "dockerfile" ||
    base.startsWith("dockerfile.") ||
    base === "makefile"
  ) {
    return "code";
  }
  if (type.startsWith("text/") || TEXT_SUFFIXES.has(suffix)) return "text";
  return "binary";
}

function workspaceArtifactLabel(kind: WorkspaceArtifactKind, path?: string) {
  if (kind === "image") return "IMG";
  if (kind === "audio") return "AUD";
  if (kind === "video") return "VID";
  if (kind === "code") return path ? workspaceArtifactExt(path).toUpperCase() || "CODE" : "CODE";
  if (kind === "text" && path && isMarkdownPath(path)) return "MD";
  if (kind === "text") return "TEXT";
  if (kind === "binary") return "BIN";
  return kind.toUpperCase();
}

function formatArtifactSize(size: WorkspaceArtifact["size"]) {
  if (typeof size !== "number" || !Number.isFinite(size)) return "unknown size";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function ArtifactPreviewBody({
  artifact,
  kind,
  url,
  text,
}: {
  artifact: WorkspaceArtifact;
  kind: WorkspaceArtifactKind;
  url: string;
  text: string | null;
}) {
  if (kind === "image") {
    return (
      <div className="flex min-h-[360px] items-center justify-center">
        <img
          src={url}
          alt={artifact.path}
          className="max-h-[72vh] max-w-full object-contain"
        />
      </div>
    );
  }

  if (kind === "svg") {
    return text !== null ? (
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="flex min-h-[320px] items-center justify-center rounded-md bg-white p-4">
          <img
            src={url}
            alt={artifact.path}
            className="max-h-[60vh] max-w-full object-contain"
          />
        </div>
        <CopyableCodeBlock text={text} label="SVG source" />
      </div>
    ) : (
      <img src={url} alt={artifact.path} className="mx-auto max-h-[72vh] max-w-full" />
    );
  }

  if (kind === "pdf") {
    return <iframe src={url} title={artifact.path} className="h-[72vh] w-full rounded-md bg-white" />;
  }

  if (kind === "audio") {
    return <audio src={url} controls className="mt-8 w-full" />;
  }

  if (kind === "video") {
    return <video src={url} controls className="mx-auto max-h-[72vh] max-w-full rounded-md" />;
  }

  if (kind === "html") {
    return (
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <iframe
          src={url}
          title={artifact.path}
          sandbox=""
          className="h-[68vh] w-full rounded-md bg-white"
        />
        {text !== null ? <CopyableCodeBlock text={text} label="HTML source" /> : <LargeTextFallback artifact={artifact} />}
      </div>
    );
  }

  if (kind === "csv" || kind === "tsv") {
    return text !== null ? (
      <DelimitedTable text={text} delimiter={kind === "csv" ? "," : "\t"} />
    ) : (
      <LargeTextFallback artifact={artifact} />
    );
  }

  if (kind === "json") {
    return text !== null ? <JsonPreview text={text} /> : <LargeTextFallback artifact={artifact} />;
  }

  if (kind === "text" && isMarkdownArtifact(artifact)) {
    return text !== null ? (
      <div className="text-[14px] leading-relaxed text-ink">
        <Suspense
          fallback={<div className="break-anywhere whitespace-pre-wrap">{text}</div>}
        >
          <MarkdownBody content={text} />
        </Suspense>
      </div>
    ) : (
      <LargeTextFallback artifact={artifact} />
    );
  }

  if (kind === "text" || kind === "code") {
    return text !== null ? (
      <CopyableCodeBlock text={text} label={workspaceArtifactLabel(kind, artifact.path)} />
    ) : (
      <LargeTextFallback artifact={artifact} />
    );
  }

  return (
    <div className="py-16 text-center text-sm text-ink-dim">
      <div className="mb-2 text-ink-soft">No inline preview for this file type.</div>
      <div>Use Open or Download to view it outside the dashboard.</div>
    </div>
  );
}

function JsonPreview({ text }: { text: string }) {
  try {
    return <CopyableCodeBlock text={JSON.stringify(JSON.parse(text), null, 2)} label="valid JSON" />;
  } catch (ex) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-signal-authority/45 bg-signal-authority/12 px-3 py-2 text-xs text-signal-authority">
          Invalid JSON: {messageFromError(ex)}
        </div>
        <CopyableCodeBlock text={text} label="raw JSON" />
      </div>
    );
  }
}

function LargeTextFallback({ artifact }: { artifact: WorkspaceArtifact }) {
  return (
    <div className="py-16 text-center text-sm text-ink-dim">
      <div className="mb-2 text-ink-soft">File is too large for inline text preview.</div>
      <div>
        {formatArtifactSize(artifact.size)} exceeds the {formatArtifactSize(TEXT_PREVIEW_LIMIT)} preview limit.
      </div>
    </div>
  );
}

function TextDiffPanel({ diff, path }: { diff: TextDiff; path: string }) {
  return (
    <div className="overflow-hidden rounded-md border border-runtime-line-soft/60 bg-runtime-panel/50">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-runtime-line-soft/60 px-3 py-2">
        <div>
          <div className="font-mono text-xs text-ink-soft">Changes since last preview</div>
          <div className="mt-0.5 truncate font-mono text-[10px] text-ink-faint">{path}</div>
        </div>
        <div className="flex items-center gap-2 font-mono text-[11px]">
          <span className="text-signal-live">+{diff.added}</span>
          <span className="text-signal-danger">−{diff.removed}</span>
        </div>
      </div>
      <div
        className="max-h-[68vh] overflow-auto font-mono text-xs leading-5"
        role="region"
        aria-label={`Text changes for ${path}`}
      >
        {diff.lines.map((line, index) => {
          if (line.kind === "skip") {
            return (
              <div
                key={`skip:${index}`}
                className="border-y border-runtime-line-soft/40 bg-runtime-bg/70 px-3 py-1 text-center text-[10px] text-ink-faint"
              >
                ··· {line.text} ···
              </div>
            );
          }
          const marker = line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " ";
          const tone =
            line.kind === "add"
              ? "bg-signal-live/10 text-signal-live"
              : line.kind === "remove"
                ? "bg-signal-danger/10 text-signal-danger"
                : "text-ink-muted";
          return (
            <div
              key={`${line.kind}:${line.beforeLine ?? ""}:${line.afterLine ?? ""}:${index}`}
              className={`grid min-w-max grid-cols-[2.75rem_2.75rem_1.25rem_minmax(24rem,1fr)] ${tone}`}
            >
              <span className="select-none border-r border-runtime-line-soft/40 px-2 text-right text-ink-faint">
                {line.beforeLine ?? ""}
              </span>
              <span className="select-none border-r border-runtime-line-soft/40 px-2 text-right text-ink-faint">
                {line.afterLine ?? ""}
              </span>
              <span className="select-none text-center" aria-hidden="true">{marker}</span>
              <span className="whitespace-pre pr-4">{line.text || " "}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function DelimitedTable({
  text,
  delimiter,
}: {
  text: string;
  delimiter: "," | "\t";
}) {
  const parsed = useMemo(() => parseDelimited(text, delimiter), [text, delimiter]);
  const rows = parsed.slice(0, TABLE_PREVIEW_ROWS + 1);
  const headers = rows[0] || [];
  const columnCount = Math.max(headers.length, ...rows.map((row) => row.length), 0);
  const columns = Array.from(
    { length: columnCount },
    (_, index) => headers[index] || `column ${index + 1}`,
  );
  const body = rows.slice(1);
  const totalRows = Math.max(parsed.length - 1, 0);

  if (!rows.length || !columnCount) {
    return (
      <div className="py-16 text-center text-sm text-ink-dim">
        <div className="mb-2 text-ink-soft">No rows found.</div>
        <div>This file is empty or only contains blank rows.</div>
      </div>
    );
  }

  return (
    <div className="overflow-auto rounded-md border border-runtime-line-soft/60">
      <div className="flex items-center justify-between gap-3 border-b border-runtime-line-soft/60 bg-runtime-panel px-3 py-2 text-xs text-ink-muted">
        <span>
          {totalRows} {totalRows === 1 ? "row" : "rows"} / {columnCount}{" "}
          {columnCount === 1 ? "column" : "columns"}
        </span>
        <span>{delimiter === "," ? "CSV" : "TSV"}</span>
      </div>
      <table className="min-w-full border-collapse text-left text-xs">
        <thead className="sticky top-0 bg-runtime-panel text-ink-soft">
          <tr>
            {columns.map((column, index) => (
              <th
                key={`${column}-${index}`}
                className="border-b border-r border-runtime-line-soft/60 px-3 py-2 font-medium"
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono text-ink-soft">
          {body.map((row, rowIndex) => (
            <tr key={rowIndex} className="odd:bg-runtime-panel/40">
              {columns.map((_, columnIndex) => (
                <td
                  key={columnIndex}
                  className="max-w-[320px] truncate border-r border-runtime-line-soft/60 px-3 py-1.5"
                  title={row[columnIndex] ?? ""}
                >
                  {row[columnIndex] ?? ""}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {parsed.length > TABLE_PREVIEW_ROWS + 1 && (
        <div className="border-t border-runtime-line-soft/60 bg-runtime-panel px-3 py-2 text-xs text-ink-muted">
          showing first {TABLE_PREVIEW_ROWS} rows
        </div>
      )}
    </div>
  );
}

function ArtifactBadge({
  kind,
  path,
}: {
  kind: WorkspaceArtifactKind;
  path: string;
}) {
  return (
    <span
      className={cx(
        "flex h-9 w-11 shrink-0 items-center justify-center rounded-md border px-1 font-mono text-[10px]",
        artifactTone(kind),
      )}
      aria-hidden="true"
    >
      {workspaceArtifactBadge(kind, path)}
    </span>
  );
}

function ArtifactMeta({ artifact }: { artifact: WorkspaceArtifact }) {
  const parts = [
    artifact.op ? artifact.op : "",
    formatArtifactSize(artifact.size),
    artifact.content_type || "",
    artifact.modified_at ? formatArtifactDate(artifact.modified_at) : "",
  ].filter(Boolean);

  return (
    <span className="mt-1 block text-[11px] text-ink-faint">
      {parts.join(" / ")}
    </span>
  );
}

function typedArtifactBlob(blob: Blob, artifact: WorkspaceArtifact) {
  return blob.type || !artifact.content_type
    ? blob
    : blob.slice(0, blob.size, artifact.content_type);
}

function parseDelimited(text: string, delimiter: "," | "\t") {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];

    if (quoted) {
      if (char === '"' && next === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === delimiter) {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (char !== "\r") {
      cell += char;
    }
  }

  row.push(cell);
  rows.push(row);
  return rows.filter((currentRow) => currentRow.some((currentCell) => currentCell.length > 0));
}

function workspaceArtifactBadge(kind: WorkspaceArtifactKind, path: string) {
  if (kind === "code") return "CODE";
  return workspaceArtifactLabel(kind, path);
}

function workspaceArtifactName(path: string) {
  return path.split("/").pop() || path;
}

function workspaceArtifactExt(path: string) {
  const base = workspaceArtifactName(path);
  const index = base.lastIndexOf(".");
  return index >= 0 ? base.slice(index + 1).toLowerCase() : "";
}

function artifactTone(kind: WorkspaceArtifactKind) {
  if (kind === "image" || kind === "svg" || kind === "video") {
    return "border-runtime-line bg-runtime-panel/70 text-ink-soft";
  }
  if (kind === "pdf" || kind === "html") {
    return "border-runtime-line bg-runtime-panel/70 text-ink-soft";
  }
  if (kind === "csv" || kind === "tsv" || kind === "json") {
    return "border-signal-live/45 bg-signal-live/12 text-signal-live";
  }
  if (kind === "audio") {
    return "border-signal-authority/45 bg-signal-authority/12 text-signal-authority";
  }
  if (kind === "code" || kind === "text") {
    return "border-runtime-line bg-runtime-panel/70 text-ink-soft";
  }
  return "border-runtime-line-soft/60 bg-runtime-bg text-ink-soft";
}

function isTextPreviewKind(kind: WorkspaceArtifactKind) {
  return ["text", "code", "json", "csv", "tsv", "svg", "html"].includes(kind);
}

function isMarkdownArtifact(artifact: WorkspaceArtifact) {
  const contentType = (artifact.content_type || "").toLowerCase();
  return contentType.includes("markdown") || isMarkdownPath(artifact.path);
}

function isMarkdownPath(path: string) {
  const suffix = workspaceArtifactExt(path);
  return suffix === "md" || suffix === "markdown";
}

function formatArtifactDate(value: string) {
  if (!value) return "unknown date";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function messageFromError(ex: unknown) {
  return ex instanceof Error ? ex.message : String(ex);
}

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}
