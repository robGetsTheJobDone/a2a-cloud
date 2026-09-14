import { useMemo, useState } from "react";
import type { SubagentFileOp } from "../api";
import { Icon } from "./Icon";
import {
  WorkspaceArtifactList,
  type WorkspaceArtifact,
} from "./WorkspaceArtifactPreview";
import { useOptionalWorkspaceInspector } from "../providers/WorkspaceInspectorContext";

export type ChatFileOp = SubagentFileOp;

const FILE_OP_ORDER = ["create", "update", "delete"] as const;

export function ChatFileOpsList({
  ops,
  defaultOpen = false,
}: {
  ops: readonly ChatFileOp[];
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const inspector = useOptionalWorkspaceInspector();
  const { allArtifacts, visibleArtifacts, summary } = useMemo(() => {
    const artifacts = ops.map(toWorkspaceArtifact);
    return {
      allArtifacts: artifacts,
      visibleArtifacts: artifacts.filter((artifact) => artifact.op !== "delete"),
      summary: summarizeFileOps(ops),
    };
  }, [ops]);
  const listArtifacts = open ? allArtifacts : visibleArtifacts;

  return (
    <div className="mt-3 min-w-0">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        className="mb-2 inline-flex items-center gap-1.5 rounded-md px-1 py-0.5 text-xs text-ink-dim hover:bg-runtime-panel/70 hover:text-ink-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/50"
      >
        <Icon
          name={open ? "chevron-down" : "chevron-right"}
          size={12}
          className="text-ink-muted"
        />
        <span>file changes</span>
        <span className="text-ink-muted">{summary}</span>
      </button>
      {listArtifacts.length > 0 && (
        <WorkspaceArtifactList
          artifacts={listArtifacts}
          variant="list"
          maxItems={open ? undefined : 6}
          showDeleted={open}
          onOpen={inspector?.openArtifact}
          hrefForArtifact={inspector ? () => "" : undefined}
        />
      )}
    </div>
  );
}

export function summarizeFileOps(ops: readonly Pick<ChatFileOp, "op">[]) {
  if (ops.length === 0) return "0 changes";

  const counts = ops.reduce((acc, op) => {
    acc.set(op.op, (acc.get(op.op) || 0) + 1);
    return acc;
  }, new Map<string, number>());
  const ordered = FILE_OP_ORDER.filter((op) => counts.has(op)).map((op) =>
    formatFileOpCount(op, counts.get(op) || 0),
  );
  const extra = Array.from(counts)
    .filter(([op]) => !FILE_OP_ORDER.includes(op as (typeof FILE_OP_ORDER)[number]))
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([op, count]) => formatFileOpCount(op, count));

  return [...ordered, ...extra].join(", ");
}

function formatFileOpCount(op: string, count: number) {
  return `${count} ${op}${count === 1 ? "" : "s"}`;
}

function toWorkspaceArtifact(op: ChatFileOp): WorkspaceArtifact {
  return {
    path: op.path,
    size: op.size,
    content_type: op.content_type,
    op: op.op,
  };
}
