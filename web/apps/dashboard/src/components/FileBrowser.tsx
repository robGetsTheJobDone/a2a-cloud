import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  deleteFile,
  listFiles,
  moveFile,
  uploadFile,
  type FileMeta,
} from "../api";
import {
  workspaceArtifactHref,
  workspaceArtifactPathFromPathname,
} from "../navigation";
import {
  Dialog,
  InlineAlert,
  ToolbarButton,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionState,
} from "./DashboardSectionCache";
import { Icon } from "./Icon";
import {
  downloadWorkspaceArtifact,
  workspaceArtifactKind,
} from "./WorkspaceArtifactPreview";

const DRAG_PATH_MIME = "application/x-a2a-path";
const LIVE_REFRESH_MS = 2_500;
const CHANGE_HIGHLIGHT_MS = 12_000;

type BusyKind = "deleting" | "moving";

type FileChangeKind = "create" | "update" | "delete";

export type FileChangeRecord = {
  kind: FileChangeKind;
  path: string;
  detectedAt: number;
  before?: FileMeta;
  after?: FileMeta;
};

type DirNode = {
  kind: "dir";
  path: string; // "" for root, "data", "data/raw"
  name: string;
  children: TreeNode[];
};

type FileNode = {
  kind: "file";
  path: string;
  name: string;
  meta: FileMeta;
};

type TreeNode = DirNode | FileNode;

function sameFileRevision(previous: FileMeta, next: FileMeta) {
  if (previous.etag && next.etag) return previous.etag === next.etag;
  return (
    previous.size === next.size &&
    previous.modified_at === next.modified_at &&
    previous.content_type === next.content_type
  );
}

export function diffFileEntries(
  previous: readonly FileMeta[],
  next: readonly FileMeta[],
  detectedAt = Date.now(),
): FileChangeRecord[] {
  const beforeByPath = new Map(
    previous.filter((file) => !file.is_dir).map((file) => [file.path, file]),
  );
  const afterByPath = new Map(
    next.filter((file) => !file.is_dir).map((file) => [file.path, file]),
  );
  const changes: FileChangeRecord[] = [];

  for (const [path, after] of afterByPath) {
    const before = beforeByPath.get(path);
    if (!before) {
      changes.push({ kind: "create", path, detectedAt, after });
    } else if (!sameFileRevision(before, after)) {
      changes.push({ kind: "update", path, detectedAt, before, after });
    }
  }
  for (const [path, before] of beforeByPath) {
    if (!afterByPath.has(path)) {
      changes.push({ kind: "delete", path, detectedAt, before });
    }
  }
  return changes;
}

function fmtSize(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fmtDate(value: string) {
  if (!value) return "unknown date";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(d);
}

function fileName(path: string) {
  return path.split("/").pop() || path;
}

function messageFromError(ex: unknown) {
  return ex instanceof Error ? ex.message : String(ex);
}

function uploadDestinationPath(name: string, prefix: string) {
  const clean = prefix.replace(/^\/+|\/+$/g, "");
  return clean ? `${clean}/${name}` : name;
}

function parentPrefix(path: string) {
  const clean = path.replace(/^\/+|\/+$/g, "");
  const idx = clean.lastIndexOf("/");
  return idx >= 0 ? clean.slice(0, idx) : "";
}

function isReadOnlyPath(path: string) {
  return path === "agents" || path.startsWith("agents/");
}

function fileKindLabel(file: FileMeta) {
  const kind = workspaceArtifactKind(file);
  if (kind === "image" || kind === "svg") return "IMG";
  if (kind === "json" || kind === "text" || kind === "code" || kind === "html") return "{}";
  if (kind === "pdf") return "PDF";
  if (kind === "csv" || kind === "tsv") return "CSV";
  if (kind === "audio") return "AUD";
  if (kind === "video") return "VID";
  return "FILE";
}

function buildTree(files: FileMeta[]): DirNode {
  const root: DirNode = { kind: "dir", path: "", name: "", children: [] };
  const dirIndex = new Map<string, DirNode>([["", root]]);

  function ensureDir(path: string) {
    const clean = path.trim().replace(/^\/+|\/+$/g, "");
    if (!clean) return root;
    let parent = root;
    let cur = "";
    for (const seg of clean.split("/").filter(Boolean)) {
      cur = cur ? `${cur}/${seg}` : seg;
      let node = dirIndex.get(cur);
      if (!node) {
        node = { kind: "dir", path: cur, name: seg, children: [] };
        dirIndex.set(cur, node);
        parent.children.push(node);
      }
      parent = node;
    }
    return parent;
  }

  for (const f of files) {
    const cleanPath = f.path.trim().replace(/^\/+|\/+$/g, "");
    if (!cleanPath) continue;
    if (f.is_dir) {
      ensureDir(cleanPath);
      continue;
    }
    const segs = cleanPath.split("/").filter(Boolean);
    if (!segs.length) continue;
    const fileName = segs.pop()!;
    const parent = ensureDir(segs.join("/"));
    parent.children.push({
      kind: "file",
      path: cleanPath,
      name: fileName,
      meta: f,
    });
  }

  function sort(node: DirNode) {
    node.children.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const c of node.children) if (c.kind === "dir") sort(c);
  }
  sort(root);
  return root;
}

function countDescendantFiles(dir: DirNode): number {
  let n = 0;
  for (const c of dir.children) {
    if (c.kind === "file") n += 1;
    else n += countDescendantFiles(c);
  }
  return n;
}

function isRowActivationKey(e: React.KeyboardEvent<HTMLElement>) {
  return e.key === "Enter" || e.key === " ";
}

export type FileBrowserProps = {
  onCollapse?: () => void;
  selectedPath?: string | null;
  onSelectFile?: (file: FileMeta) => void;
  onClearSelection?: () => void;
  onFilesChange?: (files: FileMeta[]) => void;
};

export function FileBrowser({
  onCollapse,
  selectedPath,
  onSelectFile,
  onClearSelection,
  onFilesChange,
}: FileBrowserProps = {}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [entriesByPrefix, setEntriesByPrefix] = useDashboardSectionState<
    Record<string, FileMeta[]>
  >(DASHBOARD_SECTION_CACHE_KEYS.workspace.fileBrowserEntries, {});
  const [err, setErr] = useState<string | null>(null);
  const [rootDragOver, setRootDragOver] = useState(false);
  const [hoverDir, setHoverDir] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const [expanded, setExpanded] = useDashboardSectionState<Set<string>>(
    DASHBOARD_SECTION_CACHE_KEYS.workspace.fileBrowserExpanded,
    () => new Set([""]),
  );
  const [busyPaths, setBusyPaths] = useState<Record<string, BusyKind>>({});
  const [pendingDeletePath, setPendingDeletePath] = useState<string | null>(null);
  const [recentChanges, setRecentChanges] = useState<Record<string, FileChangeRecord>>({});
  const [syncing, setSyncing] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const loadedPrefixesRef = useRef<Set<string>>(new Set(Object.keys(entriesByPrefix)));
  const inFlightPrefixesRef = useRef<Set<string>>(new Set());
  const liveErrorsRef = useRef<Map<string, string>>(new Map());
  const entriesByPrefixRef = useRef(entriesByPrefix);
  const changeTimersRef = useRef<Map<string, number>>(new Map());

  const files = useMemo(
    () => Object.values(entriesByPrefix).flat(),
    [entriesByPrefix],
  );

  const recordChanges = useCallback((changes: FileChangeRecord[]) => {
    if (!changes.length) return;
    setRecentChanges((current) => {
      const next = { ...current };
      for (const change of changes) next[change.path] = change;
      return next;
    });

    for (const change of changes) {
      const existingTimer = changeTimersRef.current.get(change.path);
      if (existingTimer) window.clearTimeout(existingTimer);
      const timer = window.setTimeout(() => {
        setRecentChanges((current) => {
          if (current[change.path]?.detectedAt !== change.detectedAt) return current;
          const next = { ...current };
          delete next[change.path];
          return next;
        });
        changeTimersRef.current.delete(change.path);
      }, CHANGE_HIGHLIGHT_MS);
      changeTimersRef.current.set(change.path, timer);
    }
  }, []);

  const loadPrefix = useCallback(
    async (
      prefix: string,
      options: { force?: boolean; detectChanges?: boolean; silent?: boolean } = {},
    ) => {
      const {
        force = false,
        detectChanges = true,
        silent = false,
      } = options;
      const clean = prefix.replace(/^\/+|\/+$/g, "");
      if (!force && loadedPrefixesRef.current.has(clean)) return;
      if (inFlightPrefixesRef.current.has(clean)) return;
      inFlightPrefixesRef.current.add(clean);
      setSyncing(true);
      try {
        const next = await listFiles(clean);
        const previous = entriesByPrefixRef.current[clean];
        if (detectChanges && previous) {
          recordChanges(diffFileEntries(previous, next));
        }
        const nextEntries = { ...entriesByPrefixRef.current, [clean]: next };
        entriesByPrefixRef.current = nextEntries;
        setEntriesByPrefix(nextEntries);
        loadedPrefixesRef.current.add(clean);
        setLastSyncedAt(Date.now());
        liveErrorsRef.current.delete(clean);
        setLiveError(liveErrorsRef.current.values().next().value ?? null);
        if (!silent) setErr(null);
      } catch (ex) {
        const message = messageFromError(ex);
        liveErrorsRef.current.set(clean, message);
        setLiveError(message);
        if (!silent) setErr(message);
      } finally {
        inFlightPrefixesRef.current.delete(clean);
        setSyncing(inFlightPrefixesRef.current.size > 0);
      }
    },
    [recordChanges, setEntriesByPrefix],
  );

  useEffect(() => {
    entriesByPrefixRef.current = entriesByPrefix;
    for (const prefix of Object.keys(entriesByPrefix)) {
      loadedPrefixesRef.current.add(prefix);
    }
  }, [entriesByPrefix]);

  useEffect(() => {
    onFilesChange?.(files.filter((file) => !file.is_dir));
  }, [files, onFilesChange]);

  useEffect(() => () => {
    for (const timer of changeTimersRef.current.values()) window.clearTimeout(timer);
    changeTimersRef.current.clear();
  }, []);

  const refreshVisible = useCallback(
    async (prefixes?: string[]) => {
      const targets = Array.from(new Set(prefixes ?? ["", ...expanded]));
      await Promise.all(
        targets.map((prefix) => loadPrefix(prefix, { force: true })),
      );
    },
    [expanded, loadPrefix],
  );

  useEffect(() => {
    void loadPrefix("", { force: true, detectChanges: true });
  }, [loadPrefix]);

  useEffect(() => {
    for (const prefix of expanded) {
      if (!loadedPrefixesRef.current.has(prefix)) {
        void loadPrefix(prefix, { detectChanges: false });
      }
    }
  }, [expanded, loadPrefix]);

  useEffect(() => {
    const refreshLiveFiles = () => {
      if (document.visibilityState === "hidden") return;
      const targets = Array.from(new Set(["", ...expanded]));
      void Promise.all(
        targets.map((prefix) =>
          loadPrefix(prefix, {
            force: true,
            detectChanges: true,
            silent: true,
          }),
        ),
      );
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") refreshLiveFiles();
    };
    const timer = window.setInterval(refreshLiveFiles, LIVE_REFRESH_MS);
    window.addEventListener("focus", refreshLiveFiles);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshLiveFiles);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [expanded, loadPrefix]);

  const tree = useMemo(() => buildTree(files), [files]);
  const filePaths = useMemo(() => new Set(files.map((f) => f.path)), [files]);
  const routeSelectedPreviewPath = workspaceArtifactPathFromPathname(
    location.pathname,
    "files",
  );
  const selectedPreviewPath =
    selectedPath !== undefined ? selectedPath : routeSelectedPreviewPath;
  const recentChangeList = useMemo(
    () => Object.values(recentChanges).sort((a, b) => b.detectedAt - a.detectedAt),
    [recentChanges],
  );

  useEffect(() => {
    if (!selectedPreviewPath) return;
    if (recentChanges[selectedPreviewPath]?.kind !== "delete") return;
    if (onClearSelection) {
      onClearSelection();
    } else {
      navigate("/workspace/files", { replace: true });
    }
  }, [navigate, onClearSelection, recentChanges, selectedPreviewPath]);

  function setPathBusy(path: string, kind: BusyKind | null) {
    setBusyPaths((current) => {
      const next = { ...current };
      if (kind) next[path] = kind;
      else delete next[path];
      return next;
    });
  }

  const openFile = useCallback(
    (file: FileMeta) => {
      if (onSelectFile) {
        onSelectFile(file);
        return;
      }
      navigate(workspaceArtifactHref(file.path, "files"));
    },
    [navigate, onSelectFile],
  );

  async function ingest(list: FileList | File[], prefix: string) {
    const arr = Array.from(list);
    if (!arr.length) return;
    const reservedPaths = new Set(filePaths);
    const skipped: string[] = [];
    const failed: string[] = [];
    setErr(null);
    setUploading(arr.length);
    try {
      for (const f of arr) {
        const dst = uploadDestinationPath(f.name, prefix);
        if (reservedPaths.has(dst)) {
          skipped.push(dst);
          setUploading((n) => Math.max(0, n - 1));
          continue;
        }
        reservedPaths.add(dst);
        try {
          await uploadFile(f, prefix);
        } catch (ex) {
          failed.push(`${dst}: ${messageFromError(ex)}`);
        } finally {
          setUploading((n) => Math.max(0, n - 1));
        }
      }
      // Auto-expand the folder we just dropped into.
      if (prefix) {
        setExpanded((s) => {
          const next = new Set(s);
          // Expand every ancestor of the prefix too.
          const parts = prefix.split("/");
          for (let i = 1; i <= parts.length; i++) {
            next.add(parts.slice(0, i).join("/"));
          }
          return next;
        });
      }
      await refreshVisible([prefix, ...expanded]);
      const notes = [
        skipped.length ? `Skipped existing file${skipped.length === 1 ? "" : "s"}: ${skipped.join(", ")}` : "",
        failed.length ? `Upload failed: ${failed.join("; ")}` : "",
      ].filter(Boolean);
      if (notes.length) setErr(notes.join(". "));
    } finally {
      setUploading(0);
    }
  }

  async function moveTo(src: string, destDir: string) {
    const fileName = src.split("/").pop() || src;
    const dst = uploadDestinationPath(fileName, destDir);
    if (src === dst) return;
    const sourceFile = files.find((file) => file.path === src && !file.is_dir);
    if (!sourceFile) {
      await refreshVisible([parentPrefix(src), destDir, ...expanded]);
      setErr(`Cannot move ${src}: file is no longer in the workspace.`);
      return;
    }
    if (filePaths.has(dst)) {
      setErr(`Cannot move ${src}: ${dst} already exists.`);
      return;
    }
    setErr(null);
    setPathBusy(src, "moving");
    try {
      await moveFile(src, dst);
      if (selectedPreviewPath === src) {
        if (onSelectFile) {
          onSelectFile({ ...sourceFile, path: dst });
        } else {
          navigate(workspaceArtifactHref(dst, "files"), { replace: true });
        }
      }
      if (destDir) {
        setExpanded((s) => {
          const next = new Set(s);
          const parts = destDir.split("/");
          for (let i = 1; i <= parts.length; i++) {
            next.add(parts.slice(0, i).join("/"));
          }
          return next;
        });
      }
      await refreshVisible([parentPrefix(src), destDir, ...expanded]);
    } catch (ex) {
      const msg = messageFromError(ex);
      if (msg.startsWith("404:")) {
        await refreshVisible([parentPrefix(src), destDir, ...expanded]);
      }
      setErr(msg);
    } finally {
      setPathBusy(src, null);
    }
  }

  // Mandate C: deletion confirms through an in-place dialog (Dialog renders as a
  // right-side sheet placement when desired) rather than a native window.confirm,
  // and never unmounts the kept-alive tree.
  function requestDelete(path: string) {
    setPendingDeletePath(path);
  }

  async function performDelete(path: string) {
    setPendingDeletePath(null);
    setErr(null);
    setPathBusy(path, "deleting");
    try {
      await deleteFile(path);
      if (selectedPreviewPath === path) {
        if (onClearSelection) {
          onClearSelection();
        } else {
          navigate("/workspace/files", { replace: true });
        }
      }
      await refreshVisible([parentPrefix(path), ...expanded]);
    } catch (ex) {
      const msg = messageFromError(ex);
      if (msg.startsWith("404:")) {
        if (selectedPreviewPath === path) {
          if (onClearSelection) {
            onClearSelection();
          } else {
            navigate("/workspace/files", { replace: true });
          }
        }
        await refreshVisible([parentPrefix(path), ...expanded]);
        setErr(`File was already removed: ${path}. List refreshed.`);
      } else {
        setErr(msg);
      }
    } finally {
      setPathBusy(path, null);
    }
  }

  function toggle(path: string) {
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function handleDrop(e: React.DragEvent, destDir: string) {
    e.preventDefault();
    e.stopPropagation();
    setHoverDir(null);
    setRootDragOver(false);
    const internal = e.dataTransfer.getData(DRAG_PATH_MIME);
    if (internal) {
      // Don't move into self or own parent (no-op).
      const parentOfSrc = internal.includes("/")
        ? internal.slice(0, internal.lastIndexOf("/"))
        : "";
      if (parentOfSrc === destDir) return;
      // Refuse to drop a folder into itself / its own subtree (we don't
      // support folder moves yet, but guard anyway).
      if (destDir === internal || destDir.startsWith(internal + "/")) return;
      void moveTo(internal, destDir);
      return;
    }
    if (e.dataTransfer.files.length) {
      void ingest(e.dataTransfer.files, destDir);
    }
  }

  return (
    <div
      className={`flex h-full min-h-0 flex-col border-r border-runtime-line-soft/60 ${
        rootDragOver ? "bg-runtime-raised/30" : "bg-runtime-bg"
      }`}
    >
      <div className="flex items-start justify-between gap-3 border-b border-runtime-line-soft/60 px-4 py-3">
        <div className="min-w-0">
          <div className="text-xs uppercase text-ink-muted">
            Workspace
          </div>
          <div className="mt-0.5 text-sm font-medium text-ink">File browser</div>
          <div className="mt-1 text-xs text-ink-muted">
            Click a file to preview its contents.
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <StatusBadge tone={files.length > 0 ? "emerald" : "neutral"}>
              {files.length} loaded
            </StatusBadge>
            <StatusBadge
              tone={liveError ? "red" : "emerald"}
              dot
              title={
                liveError
                  ? `Live refresh retrying: ${liveError}`
                  : lastSyncedAt
                    ? `Last checked ${new Date(lastSyncedAt).toLocaleTimeString()}`
                    : "Watching workspace files"
              }
            >
              {liveError ? "reconnecting" : syncing ? "syncing" : "live"}
            </StatusBadge>
            {uploading > 0 && <StatusBadge tone="amber">uploading</StatusBadge>}
            {selectedPreviewPath && <StatusBadge tone="neutral">preview</StatusBadge>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <ToolbarButton
            onClick={() => inputRef.current?.click()}
            size="sm"
            className="gap-1.5"
          >
            <Icon name="upload" size={14} />
            Upload
          </ToolbarButton>
          {onCollapse && (
            <ToolbarButton
              onClick={onCollapse}
              aria-label="Collapse files"
              title="Collapse files"
              variant="ghost"
              size="sm"
              className="h-9 w-9 px-0"
            >
              <Icon name="panel-left" size={14} />
            </ToolbarButton>
          )}
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            const selected = e.currentTarget.files ? Array.from(e.currentTarget.files) : [];
            e.currentTarget.value = "";
            if (selected.length) void ingest(selected, "");
          }}
        />
      </div>

      {err && (
        <InlineAlert
          tone="red"
          className="flex items-start gap-2 rounded-none border-x-0 border-t-0 px-4 py-2 text-xs"
          role="alert"
        >
          <span className="min-w-0 flex-1">{err}</span>
          <ToolbarButton
            onClick={() => setErr(null)}
            variant="danger"
            size="xs"
            className="h-6 w-6 shrink-0 border-transparent bg-transparent px-0"
            aria-label="Dismiss error"
            title="dismiss"
          >
            <Icon name="close" size={14} />
          </ToolbarButton>
        </InlineAlert>
      )}

      {recentChangeList.length > 0 && (
        <div
          className="border-b border-runtime-line-soft/60 bg-runtime-panel/45 px-3 py-2"
          role="status"
          aria-live="polite"
          aria-label="Live workspace file changes"
        >
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-ink-muted">
              <Icon name="sparkle" size={12} className="text-signal-live" />
              Live changes
            </span>
            <span className="text-[10px] text-ink-faint">
              {recentChangeList.length} recent
            </span>
          </div>
          <div className="space-y-1">
            {recentChangeList.slice(0, 4).map((change) => {
              const file = change.after;
              const content = (
                <>
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                    {change.path}
                  </span>
                  <StatusBadge
                    tone={
                      change.kind === "create"
                        ? "emerald"
                        : change.kind === "update"
                          ? "amber"
                          : "red"
                    }
                    className="shrink-0 uppercase"
                  >
                    {change.kind === "create" ? "new" : `${change.kind}d`}
                  </StatusBadge>
                </>
              );
              return file ? (
                <button
                  key={`${change.path}:${change.detectedAt}`}
                  type="button"
                  onClick={() => openFile(file)}
                  className="flex w-full items-center gap-2 rounded-md border border-runtime-line-soft/60 bg-runtime-bg/70 px-2 py-1.5 text-left text-ink-soft transition-colors hover:border-runtime-line-mid hover:bg-runtime-panel focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/50"
                  title={`Open ${change.path}`}
                >
                  {content}
                </button>
              ) : (
                <div
                  key={`${change.path}:${change.detectedAt}`}
                  className="flex items-center gap-2 rounded-md border border-signal-danger/35 bg-signal-danger/10 px-2 py-1.5 text-signal-danger line-through decoration-signal-danger/70"
                >
                  {content}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div
        className={`min-h-0 flex-1 overflow-auto ${
          rootDragOver ? "ring-1 ring-inset ring-runtime-line" : ""
        }`}
        onDragOver={(e) => {
          // Allow drop on root area when not over a child folder.
          if (!hoverDir) {
            e.preventDefault();
            setRootDragOver(true);
          }
        }}
        onDragLeave={(e) => {
          // Only clear when leaving the scroll container itself, not children.
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
            setRootDragOver(false);
          }
        }}
        onDrop={(e) => handleDrop(e, "")}
      >
        {!Object.prototype.hasOwnProperty.call(entriesByPrefix, "") ? (
          <div className="px-4 py-6 text-sm text-ink-muted">loading...</div>
        ) : tree.children.length === 0 ? (
          <div className="px-4 py-10 text-center text-sm text-ink-muted">
            <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-lg border border-runtime-line-soft/70 bg-runtime-panel/60 text-ink-muted">
              <Icon name="folder" size={18} />
            </div>
            <div className="mb-1 font-medium text-ink-soft">No files yet</div>
            <div className="text-xs">
              Drop files here or use Upload.
            </div>
          </div>
        ) : (
          <ul className="py-1" role="tree" aria-label="Workspace files">
            {tree.children.map((c) => (
              <Row
                key={(c.kind === "dir" ? "d:" : "f:") + c.path}
                node={c}
                depth={0}
                expanded={expanded}
                hoverDir={hoverDir}
                busyPaths={busyPaths}
                onToggle={toggle}
                onDelete={requestDelete}
                onSetHover={setHoverDir}
                onDrop={handleDrop}
                onOpenFile={openFile}
                selectedPath={selectedPreviewPath}
                recentChanges={recentChanges}
              />
            ))}
          </ul>
        )}
      </div>

      <Dialog
        open={pendingDeletePath !== null}
        onClose={() => setPendingDeletePath(null)}
        placement="right"
        size="sm"
        title="Delete file"
        description={
          pendingDeletePath
            ? `Delete ${fileName(pendingDeletePath)}? This cannot be undone.`
            : undefined
        }
        actions={
          <>
            <ToolbarButton onClick={() => setPendingDeletePath(null)} size="sm">
              Cancel
            </ToolbarButton>
            <ToolbarButton
              variant="danger"
              size="sm"
              onClick={() => {
                if (pendingDeletePath) void performDelete(pendingDeletePath);
              }}
            >
              Delete
            </ToolbarButton>
          </>
        }
      >
        {pendingDeletePath && (
          <p className="break-all font-mono text-xs text-ink-muted">
            {pendingDeletePath}
          </p>
        )}
      </Dialog>

      {uploading > 0 && (
        <div
          className="border-t border-runtime-line-soft/60 px-4 py-2 text-xs text-ink-dim"
          role="status"
          aria-live="polite"
        >
          uploading {uploading}...
        </div>
      )}
    </div>
  );
}

function Row({
  node,
  depth,
  expanded,
  hoverDir,
  busyPaths,
  onToggle,
  onDelete,
  onSetHover,
  onDrop,
  onOpenFile,
  selectedPath,
  recentChanges,
}: {
  node: TreeNode;
  depth: number;
  expanded: Set<string>;
  hoverDir: string | null;
  busyPaths: Record<string, BusyKind>;
  onToggle: (path: string) => void;
  onDelete: (path: string) => void;
  onSetHover: (path: string | null) => void;
  onDrop: (e: React.DragEvent, dest: string) => void;
  onOpenFile: (file: FileMeta) => void;
  selectedPath: string | null;
  recentChanges: Record<string, FileChangeRecord>;
}) {
  if (node.kind === "dir") {
    const isOpen = expanded.has(node.path);
    const isHover = hoverDir === node.path;
    const fileCount = countDescendantFiles(node);
    const showCount = fileCount >= 3;
    const readOnly = isReadOnlyPath(node.path);
    return (
      <li role="none">
        <div
          className={`group flex min-h-9 cursor-pointer items-center gap-1 px-2 py-2 text-sm hover:bg-runtime-panel focus-visible:bg-runtime-panel focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-runtime-line-mid sm:min-h-0 sm:py-1 ${
            isHover ? "bg-signal-authority/12 ring-1 ring-inset ring-signal-authority/70" : ""
          }`}
          style={{ paddingLeft: 8 + depth * 14 }}
          role="treeitem"
          tabIndex={0}
          aria-expanded={isOpen}
          aria-level={depth + 1}
          aria-label={`${isOpen ? "Collapse" : "Expand"} folder ${node.path || node.name}, ${fileCount} ${
            fileCount === 1 ? "file" : "files"
          }`}
          onClick={() => onToggle(node.path)}
          onKeyDown={(e) => {
            if (isRowActivationKey(e)) {
              e.preventDefault();
              onToggle(node.path);
            } else if (e.key === "ArrowRight" && !isOpen) {
              e.preventDefault();
              onToggle(node.path);
            } else if (e.key === "ArrowLeft" && isOpen) {
              e.preventDefault();
              onToggle(node.path);
            }
          }}
          onDragOver={(e) => {
            if (readOnly) return;
            e.preventDefault();
            e.stopPropagation();
            onSetHover(node.path);
          }}
          onDragLeave={(e) => {
            // Only clear if leaving this row entirely, not entering a child.
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
              onSetHover(null);
            }
          }}
          onDrop={(e) => {
            if (readOnly) return;
            e.stopPropagation();
            onDrop(e, node.path);
          }}
          draggable={false}
          title={node.path}
          >
            <span className="flex h-5 w-5 shrink-0 items-center justify-center text-ink-muted" aria-hidden="true">
              <Icon name={isOpen ? "chevron-down" : "chevron-right"} size={13} />
            </span>
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-runtime-line-soft/60 bg-runtime-bg text-ink-muted" aria-hidden="true">
              <Icon name={isOpen ? "folder-open" : "folder"} size={14} />
            </span>
            <span className="truncate text-ink-soft">{node.name}</span>
          {showCount && (
            <span className="ml-1 text-[10px] text-ink-faint">
              {fileCount} {fileCount === 1 ? "file" : "files"}
            </span>
          )}
          </div>
        {isOpen && (
          <ul role="group">
            {node.children.map((c) => (
              <Row
                key={(c.kind === "dir" ? "d:" : "f:") + c.path}
                node={c}
                depth={depth + 1}
                expanded={expanded}
                hoverDir={hoverDir}
                busyPaths={busyPaths}
                onToggle={onToggle}
                onDelete={onDelete}
                onSetHover={onSetHover}
                onDrop={onDrop}
                onOpenFile={onOpenFile}
                selectedPath={selectedPath}
                recentChanges={recentChanges}
              />
            ))}
          </ul>
        )}
      </li>
    );
  }
  // File row
  return (
    <FileRow
      node={node}
      depth={depth}
      busy={busyPaths[node.path] || null}
      onDelete={onDelete}
      onOpenFile={onOpenFile}
      selected={selectedPath === node.path}
      change={recentChanges[node.path]}
    />
  );
}

function FileRow({
  node,
  depth,
  busy,
  onDelete,
  onOpenFile,
  selected,
  change,
}: {
  node: FileNode;
  depth: number;
  busy: BusyKind | null;
  onDelete: (path: string) => void;
  onOpenFile: (file: FileMeta) => void;
  selected: boolean;
  change?: FileChangeRecord;
}) {
  const f = node.meta;
  const [downloading, setDownloading] = useState(false);
  const [downloadErr, setDownloadErr] = useState<string | null>(null);
  const readOnly = f.writable === false || isReadOnlyPath(node.path);

  async function onDownload(e: React.MouseEvent) {
    e.stopPropagation();
    if (busy) return;
    setDownloading(true);
    setDownloadErr(null);
    try {
      await downloadWorkspaceArtifact(f);
    } catch (ex) {
      setDownloadErr(messageFromError(ex));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <li role="none">
      <div
        className={`group flex min-h-11 items-center justify-between px-2 py-2 transition-colors duration-700 hover:bg-runtime-panel focus-visible:bg-runtime-panel focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-runtime-line-mid sm:min-h-0 sm:py-1 ${
          selected ? "bg-signal-authority/10 ring-1 ring-inset ring-signal-authority/45" : ""
        } ${
          change?.kind === "create"
            ? "bg-signal-live/12 ring-1 ring-inset ring-signal-live/45"
            : change?.kind === "update"
              ? "bg-signal-authority/12 ring-1 ring-inset ring-signal-authority/45"
              : ""
        } ${
          busy ? "cursor-wait opacity-70" : "cursor-pointer"
        }`}
        style={{ paddingLeft: 8 + depth * 14 + 16 }}
        role="treeitem"
        tabIndex={0}
        aria-disabled={busy ? true : undefined}
        aria-selected={selected}
        aria-level={depth + 1}
        aria-label={`${node.name}, ${fmtSize(f.size)}, ${f.content_type || "unknown"}, modified ${fmtDate(
          f.modified_at,
        )}`}
        draggable={!busy && !readOnly}
        onClick={() => {
          if (!busy) onOpenFile(f);
        }}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget || !isRowActivationKey(e)) return;
          e.preventDefault();
          if (!busy) onOpenFile(f);
        }}
        onDragStart={(e) => {
          if (busy || readOnly) {
            e.preventDefault();
            return;
          }
          e.dataTransfer.setData(DRAG_PATH_MIME, node.path);
          e.dataTransfer.effectAllowed = "move";
        }}
        title={node.path}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-runtime-line-soft/60 bg-runtime-bg text-ink-muted"
              aria-hidden="true"
            >
              <Icon name="file" size={14} />
            </span>
            <span className="truncate font-mono text-sm text-ink-soft">
              {node.name}
            </span>
            <span className="shrink-0 rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70 px-1.5 py-0.5 font-mono text-[10px] text-ink-muted">
              {fileKindLabel(f)}
            </span>
            {change && change.kind !== "delete" && (
              <StatusBadge
                tone={change.kind === "create" ? "emerald" : "amber"}
                dot
                className="shrink-0 animate-pulse uppercase"
              >
                {change.kind === "create" ? "new" : "updated"}
              </StatusBadge>
            )}
            {busy && (
              <span className="shrink-0 text-[10px] uppercase text-ink-muted">
                {busy === "moving" ? "moving" : "deleting"}
              </span>
            )}
          </div>
          <div
            className="text-[11px] text-ink-muted"
            style={{ paddingLeft: 36 }}
          >
            {fmtSize(f.size)} · {f.content_type || "unknown"} · {fmtDate(f.modified_at)}
            {readOnly && (
              <StatusBadge tone="neutral" className="ml-2 px-1.5 text-[10px] uppercase">
                read-only
              </StatusBadge>
            )}
          </div>
          {downloadErr && (
            <div className="truncate text-[11px] text-signal-danger" style={{ paddingLeft: 36 }}>
              download failed: {downloadErr}
            </div>
          )}
        </div>
        <ToolbarButton
          onClick={onDownload}
          disabled={downloading || busy !== null}
          variant="ghost"
          size="xs"
          className="ml-2 h-8 w-8 px-0 text-sm opacity-100 focus-visible:opacity-100 sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100"
          aria-label={`Download ${node.name}`}
          title="download"
        >
          {downloading ? (
            <span className="text-[10px]">...</span>
          ) : (
            <Icon name="download" size={14} />
          )}
        </ToolbarButton>
        <ToolbarButton
          onClick={(e) => {
            e.stopPropagation();
            if (!busy && !readOnly) onDelete(node.path);
          }}
          disabled={busy !== null || readOnly}
          variant="danger"
          size="xs"
          className="ml-1 h-8 w-8 border-transparent bg-transparent px-0 text-sm opacity-100 focus-visible:opacity-100 sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100"
          aria-label={`Delete ${node.name}`}
          title="delete"
        >
          <Icon name="trash" size={14} />
        </ToolbarButton>
      </div>
    </li>
  );
}
