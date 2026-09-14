import { useEffect, useState } from "react";
import {
  runTrialAgent,
  type AgentListing,
  type AgentSkill,
  type TrialRoom,
} from "../../api";
import {
  FormField,
  InlineAlert,
  SelectInput,
  SurfacePanel,
  TextArea as FormTextArea,
  ToolbarButton,
  ToolbarLink,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { type TrialAgentReadiness } from "../trialRoomUtils";
import {
  isTrialRunnableAgent,
  pluralize,
  summarizeTrialAgentReadiness,
  trialAgentReadinessContent,
  type RoomPatch,
} from "./helpers";

export function RunAgentPanel({
  room,
  agents,
  onRoomChange,
  onRefreshAgents,
  agentRefreshBusy,
  agentRefreshErr,
  agentRefreshMessage,
}: {
  room: TrialRoom;
  agents: AgentListing[];
  onRoomChange: RoomPatch;
  onRefreshAgents: () => void;
  agentRefreshBusy: boolean;
  agentRefreshErr: string | null;
  agentRefreshMessage: string | null;
}) {
  const readiness = summarizeTrialAgentReadiness(agents);
  const runnable = agents.filter(isTrialRunnableAgent);
  const [agentName, setAgentName] = useState("");
  const [skillName, setSkillName] = useState("");
  const [argsJson, setArgsJson] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const selectedAgent = runnable.find((agent) => agent.name === agentName) || runnable[0];
  const skills = selectedAgent?.card?.skills || [];
  const selectedSkill = skills.find((skill) => skill.name === skillName) || skills[0];

  useEffect(() => {
    if (!agentName && runnable[0]) setAgentName(runnable[0].name);
  }, [agentName, runnable]);

  useEffect(() => {
    if (selectedAgent && !skills.some((skill) => skill.name === skillName)) {
      setSkillName(skills[0]?.name || "");
    }
  }, [selectedAgent, skillName, skills]);

  async function run() {
    if (!selectedAgent || !selectedSkill || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const input: {
        agent_name: string;
        skill_name: string;
        args_json?: string;
      } = {
        agent_name: selectedAgent.name,
        skill_name: selectedSkill.name,
      };
      if (argsJson.trim()) {
        JSON.parse(argsJson);
        input.args_json = argsJson;
      }
      onRoomChange(await runTrialAgent(room.slug, input));
      setArgsJson("");
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SurfacePanel as="section" className="min-w-0 bg-runtime-bg p-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase text-ink-muted">
            Run a candidate
          </div>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-dim">
            The agent receives only this room's scoped files and instructions.
            The result becomes a receipt you can score, review, and select.
          </p>
        </div>
      </div>
      {readiness.runnable > 0 ? (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <FormField label="Agent" className="w-full min-w-0 flex-1 sm:min-w-[220px]">
              <SelectInput
                value={selectedAgent?.name || ""}
                onChange={(e) => {
                  setAgentName(e.target.value);
                  setSkillName("");
                }}
              >
                {runnable.map((agent) => (
                  <option key={agent.id} value={agent.name}>
                    {agent.name}
                  </option>
                ))}
              </SelectInput>
            </FormField>
            <FormField label="Tool" className="w-full min-w-0 flex-1 sm:min-w-[180px]">
              <SelectInput
                value={selectedSkill?.name || ""}
                onChange={(e) => setSkillName(e.target.value)}
              >
                {skills.map((skill: AgentSkill) => (
                  <option key={skill.name} value={skill.name}>
                    {skill.name}
                  </option>
                ))}
              </SelectInput>
            </FormField>
            <ToolbarButton
              onClick={run}
              disabled={busy || !selectedAgent || !selectedSkill}
              aria-busy={busy}
              variant="primary"
              size="md"
              className="w-full sm:w-auto"
            >
              {busy ? "running..." : "run trial"}
            </ToolbarButton>
          </div>
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-ink-muted hover:text-ink-soft">
              override args JSON
            </summary>
            <FormTextArea
              value={argsJson}
              onChange={(e) => setArgsJson(e.target.value)}
              placeholder="Leave empty to let A2Acloud build args from the room."
              mono
              compact
              className="mt-2 h-28"
            />
          </details>
        </>
      ) : (
        <TrialAgentReadinessNotice
          readiness={readiness}
          refreshing={agentRefreshBusy}
          onRefresh={onRefreshAgents}
        />
      )}
      {err && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          {err}
        </InlineAlert>
      )}
      {agentRefreshErr && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          Could not refresh candidate agents. {agentRefreshErr}
        </InlineAlert>
      )}
      {agentRefreshMessage && !agentRefreshErr && (
        <InlineAlert
          tone={readiness.runnable > 0 ? "emerald" : "neutral"}
          role="status"
          className="mt-3 text-xs"
        >
          {agentRefreshMessage}
        </InlineAlert>
      )}
    </SurfacePanel>
  );
}

function TrialAgentReadinessNotice({
  readiness,
  refreshing,
  onRefresh,
}: {
  readiness: TrialAgentReadiness;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const content = trialAgentReadinessContent(readiness);
  const stats = [
    pluralize(readiness.total, "agent"),
    `${readiness.publicRunning.toLocaleString()} public running`,
    readiness.publicRunningWithoutSkills > 0
      ? `${readiness.publicRunningWithoutSkills.toLocaleString()} missing tools`
      : null,
    readiness.privateRunning > 0
      ? `${readiness.privateRunning.toLocaleString()} private running`
      : null,
    readiness.publicOffline > 0
      ? `${readiness.publicOffline.toLocaleString()} public offline`
      : null,
  ].filter((item): item is string => Boolean(item));

  return (
    <div className="border-t border-runtime-line-soft/60 pt-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <StatusBadge tone="amber" dot>
            run blocked
          </StatusBadge>
          <h3 className="mt-2 text-base font-semibold text-ink">
            {content.title}
          </h3>
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-ink-muted">
            {content.detail}
          </p>
          <div className="mt-3 flex min-w-0 flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-ink-faint">
            {stats.map((item) => (
              <span key={item} className="max-w-full truncate">
                {item}
              </span>
            ))}
          </div>
        </div>
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
          <ToolbarButton
            onClick={onRefresh}
            disabled={refreshing}
            aria-busy={refreshing}
            size="md"
            className="w-full sm:w-auto"
          >
            {refreshing ? "refreshing..." : "Refresh candidates"}
          </ToolbarButton>
          <ToolbarLink
            href={content.href}
            variant="primary"
            size="md"
            className="w-full justify-center sm:w-auto"
          >
            {content.action}
          </ToolbarLink>
        </div>
      </div>
    </div>
  );
}
