import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createTrialRoom,
  listFiles,
  type FileMeta,
  type TrialRoom,
} from "../../api";
import {
  FormField,
  InlineAlert,
  SurfacePanel,
  TextArea as FormTextArea,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { fmtBytes } from "../trialRoomUtils";
import { parseWorkspacePaths } from "./helpers";

/**
 * NewTrialRoomPage — body of the create-trial DetailSheet/Dialog (mandate C).
 * The right-side sheet shell supplies the title + close affordance; this owns
 * the form fields and submit logic. State (drafts) persists while the sheet is
 * open and is only torn down on close/create.
 */
export function NewTrialRoomPage({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (room: TrialRoom) => void;
}) {
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [criteria, setCriteria] = useState("");
  const [paths, setPaths] = useState("");
  const [schema, setSchema] = useState(
    "{\n  \"type\": \"object\",\n  \"required\": [\"summary\"],\n  \"properties\": {\n    \"summary\": { \"type\": \"string\" }\n  }\n}",
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputPaths = useMemo(() => parseWorkspacePaths(paths), [paths]);

  async function create() {
    setBusy(true);
    setErr(null);
    try {
      const outputSchema = schema.trim() ? JSON.parse(schema) : {};
      onCreated(await createTrialRoom({
        title,
        goal,
        acceptance_criteria: criteria,
        input_paths: inputPaths,
        output_schema: outputSchema,
        max_runtime_seconds: 300,
      }));
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-w-0">
      <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 pb-4 sm:flex-row sm:items-start sm:justify-between">
        <p className="max-w-2xl text-sm leading-relaxed text-ink-muted">
          Define the goal, files, acceptance rules, and receipt shape before running candidate agents.
        </p>
        <div className="flex shrink-0 flex-wrap gap-2">
          <ToolbarButton type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </ToolbarButton>
          <ToolbarButton
            type="button"
            variant="primary"
            onClick={create}
            disabled={busy || !title.trim() || !goal.trim()}
            aria-busy={busy}
          >
            {busy ? "Creating..." : "Create trial"}
          </ToolbarButton>
        </div>
      </div>

      <div className="mt-4 space-y-4">
          <FormField label="Title">
            <TextInput
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="May invoice reconciliation"
            />
          </FormField>
          <FormField label="Goal">
            <FormTextArea
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              rows={5}
              placeholder="Describe the work exactly as the buyer would request it, including the expected deliverable."
            />
          </FormField>
          <WorkspaceFileSelector
            value={inputPaths}
            onChange={(next) => setPaths(next.join("\n"))}
          />
          <FormField label="Workspace file paths">
            <FormTextArea
              value={paths}
              onChange={(event) => setPaths(event.target.value)}
              rows={3}
              placeholder={"invoices/january.pdf\ndata/customers.csv"}
            />
          </FormField>
          <FormField label="Acceptance criteria">
            <FormTextArea
              value={criteria}
              onChange={(event) => setCriteria(event.target.value)}
              rows={4}
              placeholder="What would make this a pass? Mention required fields, files, formats, and review rules."
            />
          </FormField>
          <FormField label="Output schema JSON">
            <FormTextArea
              value={schema}
              onChange={(event) => setSchema(event.target.value)}
              rows={6}
              mono
            />
          </FormField>
        </div>

      {err && (
        <div role="alert" className="mt-4">
          <InlineAlert tone="red">{err}</InlineAlert>
        </div>
      )}
    </div>
  );
}

function WorkspaceFileSelector({
  value,
  onChange,
}: {
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const [files, setFiles] = useState<FileMeta[] | null>(null);
  const [query, setQuery] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setFiles(await listFiles());
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const selected = useMemo(() => new Set(value), [value]);
  const visibleFiles = useMemo(() => {
    const q = query.trim().toLowerCase();
    return [...(files || [])]
      .sort((a, b) => a.path.localeCompare(b.path))
      .filter((file) => {
        if (!q) return true;
        return (
          file.path.toLowerCase().includes(q) ||
          file.content_type.toLowerCase().includes(q)
        );
      });
  }, [files, query]);
  const filePaths = useMemo(
    () => new Set((files || []).map((file) => file.path)),
    [files],
  );
  const manualPaths = files ? value.filter((path) => !filePaths.has(path)) : [];

  function toggle(path: string) {
    const next = selected.has(path)
      ? value.filter((item) => item !== path)
      : [...value, path];
    onChange(next);
  }

  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-ink-muted">Workspace files</span>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-ink-faint">{value.length} selected</span>
          <ToolbarButton
            type="button"
            onClick={refresh}
            size="xs"
          >
            refresh
          </ToolbarButton>
          {value.length > 0 && (
            <ToolbarButton
              type="button"
              onClick={() => onChange([])}
              size="xs"
            >
              clear
            </ToolbarButton>
          )}
        </div>
      </div>
      <SurfacePanel as="div" className="mt-1 bg-runtime-panel">
        <div className="border-b border-runtime-line-soft/60 p-2">
          <TextInput
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search files"
            aria-label="Search workspace files"
          />
        </div>
        <div className="max-h-56 overflow-auto">
          {files === null ? (
            <div
              role="status"
              aria-live="polite"
              className="px-3 py-3 text-sm text-ink-muted"
            >
              loading...
            </div>
          ) : files.length === 0 ? (
            <div className="px-3 py-3 text-sm text-ink-muted">
              No workspace files found.
            </div>
          ) : visibleFiles.length === 0 ? (
            <div className="px-3 py-3 text-sm text-ink-muted">
              No matching files.
            </div>
          ) : (
            visibleFiles.map((file) => (
              <label
                key={file.path}
                className="flex cursor-pointer items-start gap-3 border-b border-runtime-line-soft px-3 py-2 last:border-b-0 hover:bg-runtime-raised/60"
              >
                <input
                  type="checkbox"
                  checked={selected.has(file.path)}
                  onChange={() => toggle(file.path)}
                  className="mt-0.5 h-3.5 w-3.5 accent-ink-muted"
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-xs text-ink-soft">
                    {file.path}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-ink-faint">
                    {fmtBytes(file.size)} · {file.content_type}
                  </span>
                </span>
              </label>
            ))
          )}
        </div>
      </SurfacePanel>
      {manualPaths.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {manualPaths.map((path) => (
            <ToolbarButton
              key={path}
              type="button"
              onClick={() => toggle(path)}
              size="xs"
              className="max-w-full truncate font-mono"
              title={path}
            >
              {path}
            </ToolbarButton>
          ))}
        </div>
      )}
      {err && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
          {err}
        </InlineAlert>
      )}
    </div>
  );
}
