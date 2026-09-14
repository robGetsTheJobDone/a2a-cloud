import { Suspense, lazy, memo, useMemo } from "react";
import {
  WorkspaceArtifactList,
  type WorkspaceArtifact,
} from "./WorkspaceArtifactPreview";
import { useOptionalWorkspaceInspector } from "../providers/WorkspaceInspectorContext";

const MarkdownBody = lazy(() => import("./ChatMarkdownBody"));

type WorkspaceFileRef = {
  path: string;
};

export type ChatMessageContentProps = {
  content: string;
  className?: string;
  markdown?: boolean;
};

const WORKSPACE_PREFIXES = ["inputs", "outputs", "data", "artifacts"];
const CODE_SUFFIXES = new Set([
  "bash",
  "c",
  "cc",
  "cpp",
  "cs",
  "css",
  "dockerfile",
  "go",
  "h",
  "hpp",
  "html",
  "ini",
  "java",
  "js",
  "jsx",
  "json",
  "log",
  "md",
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
  "txt",
  "xml",
  "yaml",
  "yml",
  "zsh",
]);
const ROOT_FILE_SUFFIXES = new Set([
  ...CODE_SUFFIXES,
  "avif",
  "bin",
  "bmp",
  "csv",
  "gif",
  "gz",
  "jpeg",
  "jpg",
  "mov",
  "mp3",
  "mp4",
  "pdf",
  "png",
  "tar",
  "tsv",
  "wav",
  "webm",
  "webp",
  "zip",
]);
const EMAIL_LIKE_RE = /^[^\s@/<>`"']+@[^\s@/<>`"']+\.[^\s@/<>`"']+$/;

export const ChatMessageContent = memo(function ChatMessageContent({
  content,
  className = "",
  markdown = true,
}: ChatMessageContentProps) {
  const refs = useMemo(() => extractWorkspaceFileRefs(content), [content]);

  return (
    <div className={className}>
      {markdown ? (
        <Suspense
          fallback={<div className="break-anywhere whitespace-pre-wrap">{content}</div>}
        >
          <MarkdownBody content={content} />
        </Suspense>
      ) : (
        <div className="break-anywhere whitespace-pre-wrap">{content}</div>
      )}
      {refs.length > 0 && <WorkspaceFileCards refs={refs} />}
    </div>
  );
});

function WorkspaceFileCards({ refs }: { refs: WorkspaceFileRef[] }) {
  const inspector = useOptionalWorkspaceInspector();
  const artifacts = refs.map<WorkspaceArtifact>((ref) => ({ path: ref.path }));

  return (
    <WorkspaceArtifactList
      artifacts={artifacts}
      className="mt-3"
      variant="card"
      onOpen={inspector?.openArtifact}
      hrefForArtifact={inspector ? () => "" : undefined}
    />
  );
}

export function extractWorkspaceFileRefs(content: string): WorkspaceFileRef[] {
  const refs = new Map<string, WorkspaceFileRef>();

  for (const raw of backtickedCandidates(content)) {
    addWorkspaceFileRef(refs, raw, true);
  }
  for (const raw of plainCandidates(content)) {
    addWorkspaceFileRef(refs, raw, false);
  }

  return Array.from(refs.values());
}

function backtickedCandidates(content: string) {
  const values: string[] = [];
  const re = /`([^`]+)`/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(content))) values.push(match[1]);
  return values;
}

function plainCandidates(content: string) {
  const values: string[] = [];
  const patterns = [
    /\bworkspace:\/\/[^\s`<>"']+/gi,
    /(?:^|[\s([{"'])((?:\/workspace\/|\.a2a\/workspace\/|\/?(?:inputs|outputs|data|artifacts)\/)[^\s`<>"']+)/gi,
  ];

  for (const re of patterns) {
    let match: RegExpExecArray | null;
    while ((match = re.exec(content))) values.push(match[1] || match[0]);
  }

  return values;
}

function addWorkspaceFileRef(
  refs: Map<string, WorkspaceFileRef>,
  raw: string,
  allowRootFile: boolean,
) {
  const path = normalizeWorkspaceFileRef(raw, allowRootFile);
  if (!path || refs.has(path)) return;
  refs.set(path, { path });
}

function normalizeWorkspaceFileRef(raw: string, allowRootFile: boolean) {
  let value = trimRef(raw);
  if (!value || /[\r\n]/.test(value)) return null;

  const hadWorkspaceMarker =
    /^workspace:\/\//i.test(value) ||
    /^\/workspace\//i.test(value) ||
    /^\.a2a\/workspace\//i.test(value);

  value = value
    .replace(/^workspace:\/\/\/?/i, "")
    .replace(/^\/workspace\//i, "")
    .replace(/^\.a2a\/workspace\//i, "");

  if (value.startsWith("/")) value = value.slice(1);
  value = trimRef(value);
  if (!value || value.endsWith("/") || value.includes("://")) return null;
  if (value.split("/").some((segment) => segment === "..")) return null;

  const firstSegment = value.split("/", 1)[0]?.toLowerCase();
  const hasWorkspacePrefix = WORKSPACE_PREFIXES.includes(firstSegment);
  if (!hadWorkspaceMarker && !hasWorkspacePrefix && !allowRootFile) return null;
  if (!hadWorkspaceMarker && !hasWorkspacePrefix && !looksLikeRootFile(value)) return null;

  return value;
}

function trimRef(value: string) {
  return value
    .trim()
    .replace(/^[`"'(<[{]+/, "")
    .replace(/[`"',.;:!?>)}\]]+$/, "");
}

function looksLikeRootFile(path: string) {
  const suffix = artifactExt(path);
  return (
    !path.includes("/") &&
    !EMAIL_LIKE_RE.test(path) &&
    suffix.length > 0 &&
    ROOT_FILE_SUFFIXES.has(suffix)
  );
}

function artifactExt(path: string) {
  const base = path.split("/").pop() || path;
  const idx = base.lastIndexOf(".");
  return idx >= 0 ? base.slice(idx + 1).toLowerCase() : "";
}


