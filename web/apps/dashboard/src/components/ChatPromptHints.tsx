import { StatusBadge } from "./StatusPillAdapters";
import { Icon } from "./Icon";

type FilePresence = "attached" | "available" | "empty";

type PromptHint = {
  label: string;
  prompt: string;
};

type PromptGroup = {
  title: string;
  prompts: PromptHint[];
};

export type ChatPromptHintsProps = {
  hasAttachedFiles?: boolean;
  hasAvailableFiles?: boolean;
  attachedFileCount?: number | null;
  availableFileCount?: number | null;
  availableFileCountHasMore?: boolean;
  disabled?: boolean;
  className?: string;
  maxPrompts?: number;
  onPickPrompt: (prompt: string) => void;
};

const PROMPT_GROUPS: Record<FilePresence, PromptGroup> = {
  attached: {
    title: "Ask about attached files",
    prompts: [
      {
        label: "Summarize files",
        prompt:
          "Summarize the attached files. Include key points, risks, missing context, and recommended next steps.",
      },
      {
        label: "Extract action items",
        prompt:
          "Extract action items from the attached files. Include owners, deadlines, dependencies, and open questions when present.",
      },
      {
        label: "Compare and flag conflicts",
        prompt:
          "Compare the attached files. Flag inconsistencies, duplicates, and places that need a decision.",
      },
      {
        label: "Draft an edit plan",
        prompt:
          "Review the attached files and propose a concise edit plan. Group changes by file and explain the tradeoffs.",
      },
    ],
  },
  available: {
    title: "Ask about workspace files",
    prompts: [
      {
        label: "Find relevant files",
        prompt:
          "Scan my available workspace files and identify the highest-value files to inspect first for this task. Summarize why each matters.",
      },
      {
        label: "Review for risks",
        prompt:
          "Review the available workspace files for risks, stale assumptions, and missing follow-up. Prioritize concrete findings.",
      },
      {
        label: "Draft a status report",
        prompt:
          "Use the available workspace files to draft a brief status report with evidence, blockers, and next actions.",
      },
      {
        label: "Suggest data checks",
        prompt:
          "Look for data files in the workspace and suggest useful charts, checks, or transformations.",
      },
    ],
  },
  empty: {
    title: "Start with a task",
    prompts: [
      {
        label: "Plan needed files",
        prompt:
          "Help me decide which files to upload for this goal. Ask only for the missing context you need.",
      },
      {
        label: "Draft a project brief",
        prompt:
          "Turn my goal into a concise project brief with scope, constraints, deliverables, and acceptance checks.",
      },
      {
        label: "Make a review checklist",
        prompt:
          "Create a practical review checklist for the files I am about to provide.",
      },
      {
        label: "Break down the task",
        prompt:
          "Break this task into a short execution plan with assumptions, risks, and first steps.",
      },
    ],
  },
};

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function hasFiles(count: number | null | undefined, explicit?: boolean) {
  return explicit ?? (typeof count === "number" && count > 0);
}

function fileLabel(
  mode: FilePresence,
  attachedFileCount: number | null | undefined,
  availableFileCount: number | null | undefined,
  availableFileCountHasMore: boolean,
) {
  if (mode === "attached") {
    if (typeof attachedFileCount === "number" && attachedFileCount > 0) {
      return `${attachedFileCount} attached ${
        attachedFileCount === 1 ? "file" : "files"
      }`;
    }
    return "attached files";
  }

  if (mode === "available") {
    if (typeof availableFileCount === "number" && availableFileCount > 0) {
      const count = `${availableFileCount}${availableFileCountHasMore ? "+" : ""}`;
      return `${count} workspace ${
        availableFileCount === 1 && !availableFileCountHasMore ? "file" : "files"
      }`;
    }
    return "workspace files";
  }

  return "no files in scope";
}

export function ChatPromptHints({
  hasAttachedFiles,
  hasAvailableFiles,
  attachedFileCount,
  availableFileCount,
  availableFileCountHasMore = false,
  disabled = false,
  className,
  maxPrompts = 4,
  onPickPrompt,
}: ChatPromptHintsProps) {
  const filePresence: FilePresence = hasFiles(attachedFileCount, hasAttachedFiles)
    ? "attached"
    : hasFiles(availableFileCount, hasAvailableFiles)
      ? "available"
      : "empty";
  const group = PROMPT_GROUPS[filePresence];
  const prompts = group.prompts.slice(0, Math.max(0, maxPrompts));

  return (
    <section
      className={cx("w-full text-left", className)}
      aria-label="Prompt suggestions"
    >
      <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-mono text-[11px] font-medium uppercase tracking-[0.12em] text-ink-faint">
          {group.title}
        </h2>
        <StatusBadge tone="neutral" className="rounded-md">
          {fileLabel(
            filePresence,
            attachedFileCount,
            availableFileCount,
            availableFileCountHasMore,
          )}
        </StatusBadge>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {prompts.map((hint) => (
          <button
            key={hint.label}
            type="button"
            disabled={disabled}
            onClick={() => onPickPrompt(hint.prompt)}
            className="group flex w-full items-center gap-2.5 rounded-xl border border-runtime-line-soft/70 bg-runtime-panel/55 px-3 py-2.5 text-left transition hover:border-signal-protocol/45 hover:bg-runtime-panel/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/40 focus-visible:ring-offset-2 focus-visible:ring-offset-runtime-bg disabled:cursor-not-allowed disabled:opacity-50"
            aria-label={`Use prompt: ${hint.label}`}
          >
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-runtime-line-soft/70 bg-runtime-bg/70 text-ink-faint transition group-hover:border-signal-protocol/40 group-hover:text-signal-protocol">
              <Icon name="sparkle" size={14} />
            </span>
            <span className="min-w-0 flex-1 truncate text-sm font-medium leading-5 text-ink-soft transition group-hover:text-ink">
              {hint.label}
            </span>
            <Icon
              name="arrow-right"
              size={14}
              className="shrink-0 text-ink-faint opacity-0 transition group-hover:translate-x-0.5 group-hover:text-signal-protocol group-hover:opacity-100"
            />
          </button>
        ))}
      </div>
    </section>
  );
}
