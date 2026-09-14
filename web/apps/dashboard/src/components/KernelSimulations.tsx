import {
  EmptyState,
  InlineAlert,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import { RoutePageShell } from "./RoutePageShell";
import { DetailSheet, ListDetailLayout } from "./ListDetailLayout";
import { useSimulationsData } from "./kernel-simulations/useSimulationsData";
import {
  evolutionProposal,
  resultVariants,
  runResult,
} from "./kernel-simulations/simulationModel";
import { SimulationLabPosture } from "./kernel-simulations/SimulationPosture";
import {
  SimulationBuilderPanel,
  SimulationEvolutionLabPanel,
  SimulationSpecEditorPanel,
  SimulationTemplateListPanel,
} from "./kernel-simulations/SimulationControls";
import {
  SimulationEvolutionRunList,
  SimulationRunList,
} from "./kernel-simulations/SimulationList";
import {
  SimulationEvolutionDetail,
  SimulationRunDetail,
} from "./kernel-simulations/SimulationDetail";

/**
 * KernelSimulations — thin composer over components/kernel-simulations/*.
 *
 * Redesign (mandates A–E): the surface posture, view selector, and next-best
 * action collapse into one compact strip. Each view renders into a keep-alive
 * ListDetailLayout (left = active list / form, right = spec editor) instead of
 * the old hidden-div grid. Run + evolution detail open IN PLACE as a right-side
 * DetailSheet driven by hoisted selectedRunId / selectedEvolutionRunId state —
 * never a full-page route redirect that unmounts the list. Builder, evolution,
 * and spec form state already live in the section cache (useSimulationsData),
 * so they survive list/tab swaps untouched.
 */
export function KernelSimulations() {
  const sim = useSimulationsData();
  const {
    activeView,
    setActiveView,
    setSelectedRunId,
    setSelectedEvolutionRunId,
    setSelectedRunSection,
    templates,
    runs,
    evolutionRuns,
    availableAgents,
    selectedAgents,
    selectedId,
    setSelectedId,
    builderKind,
    setBuilderKind,
    builderTitle,
    setBuilderTitle,
    builderGoal,
    setBuilderGoal,
    executionMode,
    setExecutionMode,
    selectedAgentNames,
    specText,
    setSpecText,
    evolutionTitle,
    setEvolutionTitle,
    evolutionSeed,
    setEvolutionSeed,
    evolutionVariantCount,
    setEvolutionVariantCount,
    evolutionMaxDelta,
    setEvolutionMaxDelta,
    evolutionParticipantsText,
    setEvolutionParticipantsText,
    busy,
    evolutionBusy,
    replayBusy,
    evolutionReplayBusy,
    directRunLoadingId,
    directEvolutionRunLoadingId,
    replayResult,
    evolutionReplayResult,
    error,
    selected,
    selectedRunId,
    selectedEvolutionRunId,
    selectedRunSection,
    selectedRun,
    selectedEvolutionRun,
    trace,
    activeLiveInvocations,
    runSummary,
    evolutionSummary,
    specReadiness,
    activeRunElapsed,
    resourceLoading,
    refreshing,
    refreshSimulationData,
    toggleAgent,
    buildFromAgents,
    restoreSelectedTemplateSpec,
    runSelected,
    replayActive,
    runEvolutionExperiment,
    replayActiveEvolution,
  } = sim;

  const evolutionResult = runResult(selectedEvolutionRun);
  const evolutionVariants = resultVariants(evolutionResult);
  const proposal = evolutionProposal(evolutionResult);

  // Run detail sheet (mandate C): open whenever a run is selected in the runs
  // view; closing clears selection but keeps the list mounted.
  const runSheetOpen = activeView === "runs" && Boolean(selectedRunId);
  const evolutionSheetOpen =
    activeView === "evolution-results" && Boolean(selectedEvolutionRunId);

  const body = (() => {
    if (activeView === "builder") {
      return (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="mx-auto max-w-2xl">
            <SimulationBuilderPanel
              builderKind={builderKind}
              setBuilderKind={setBuilderKind}
              builderTitle={builderTitle}
              setBuilderTitle={setBuilderTitle}
              builderGoal={builderGoal}
              setBuilderGoal={setBuilderGoal}
              availableAgents={availableAgents}
              selectedAgentNames={selectedAgentNames}
              toggleAgent={toggleAgent}
              selectedAgentCount={selectedAgents.length}
              resourceLoading={resourceLoading}
              onBuild={buildFromAgents}
            />
          </div>
        </div>
      );
    }

    if (activeView === "evolution") {
      return (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="mx-auto max-w-2xl">
            <SimulationEvolutionLabPanel
              evolutionTitle={evolutionTitle}
              setEvolutionTitle={setEvolutionTitle}
              evolutionSeed={evolutionSeed}
              setEvolutionSeed={setEvolutionSeed}
              evolutionVariantCount={evolutionVariantCount}
              setEvolutionVariantCount={setEvolutionVariantCount}
              evolutionMaxDelta={evolutionMaxDelta}
              setEvolutionMaxDelta={setEvolutionMaxDelta}
              evolutionParticipantsText={evolutionParticipantsText}
              setEvolutionParticipantsText={setEvolutionParticipantsText}
              evolutionBusy={evolutionBusy}
              onRunEvolution={runEvolutionExperiment}
            />
          </div>
        </div>
      );
    }

    if (activeView === "spec") {
      return (
        <ListDetailLayout
          listLabel="Simulation templates"
          listWidth="340px"
          list={
            <SimulationTemplateListPanel
              templates={templates}
              selectedId={selectedId}
              setSelectedId={setSelectedId}
              resourceLoading={resourceLoading}
              refreshing={refreshing}
              onRefresh={refreshSimulationData}
            />
          }
          detail={
            <div className="p-3">
              <SimulationSpecEditorPanel
                selected={selected}
                executionMode={executionMode}
                setExecutionMode={setExecutionMode}
                onRun={runSelected}
                busy={busy}
                specReadiness={specReadiness}
                activeRunElapsed={activeRunElapsed}
                resourceLoading={resourceLoading}
                refreshing={refreshing}
                onRestoreTemplate={restoreSelectedTemplateSpec}
                onOpenBuilder={() => setActiveView("builder")}
                specText={specText}
                setSpecText={setSpecText}
              />
            </div>
          }
        />
      );
    }

    if (activeView === "evolution-results") {
      return (
        <ListDetailLayout
          listLabel="Evolution runs"
          list={
            <SimulationEvolutionRunList
              evolutionRuns={evolutionRuns}
              selectedEvolutionRunId={selectedEvolutionRunId}
              onSelect={setSelectedEvolutionRunId}
            />
          }
          detail={null}
          empty={
            <EmptyState
              size="compact"
              title="Open an evolution result"
              description="Choose an evolution run to inspect variants, winner, replay status, and proposal metadata."
            />
          }
        />
      );
    }

    // runs view (default for the right side)
    return (
      <ListDetailLayout
        listLabel="Simulation runs"
        list={
          <SimulationRunList
            runs={runs}
            selectedRunId={selectedRunId}
            onSelect={(jobId) => {
              setSelectedRunSection("overview");
              setSelectedRunId(jobId);
            }}
          />
        }
        detail={null}
        empty={
          <EmptyState
            size="compact"
            title="Open a simulation run"
            description="Choose a recent run to inspect replay evidence, live calls, and trace payloads."
          />
        }
      />
    );
  })();

  return (
    <RoutePageShell
      routeId="simulations"
      actions={<StatusBadge tone="amber">experimental</StatusBadge>}
      className="text-ink"
      layout="full-height"
    >
      <SimulationLabPosture
        activeView={activeView}
        setActiveView={setActiveView}
        templates={templates}
        selectedAgentCount={selectedAgentNames.length}
        runSummary={runSummary}
        evolutionSummary={evolutionSummary}
        selectedRunId={selectedRunId}
        selectedEvolutionRunId={selectedEvolutionRunId}
        busy={busy}
        evolutionBusy={evolutionBusy}
        activeRunElapsed={activeRunElapsed}
      />

      {error && <InlineAlert tone="red">{error}</InlineAlert>}

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-runtime-line-soft/70 bg-runtime-bg">
        {body}
      </div>

      <DetailSheet
        open={runSheetOpen}
        onClose={() => setSelectedRunId("")}
        title="Simulation run"
        description={selectedRunId || undefined}
        size="lg"
      >
        {selectedRun ? (
          <SimulationRunDetail
            run={selectedRun}
            activeSection={selectedRunSection}
            onSelectSection={setSelectedRunSection}
            liveInvocations={activeLiveInvocations}
            trace={trace}
            replayResult={replayResult}
            replayBusy={replayBusy}
            onReplay={replayActive}
          />
        ) : directRunLoadingId === selectedRunId ? (
          <EmptyState
            size="compact"
            title="Loading simulation run"
            description="Fetching owner-scoped evidence for this direct run link."
          />
        ) : (
          <EmptyState
            size="compact"
            title="Simulation run unavailable"
            description="This run could not be loaded from your accessible simulation history."
          />
        )}
      </DetailSheet>

      <DetailSheet
        open={evolutionSheetOpen}
        onClose={() => setSelectedEvolutionRunId("")}
        title="Evolution result"
        description={selectedEvolutionRunId || undefined}
        size="lg"
      >
        <SimulationEvolutionDetail
          selectedEvolutionRun={selectedEvolutionRun}
          selectedEvolutionRunId={selectedEvolutionRunId}
          evolutionResult={evolutionResult}
          evolutionVariants={evolutionVariants}
          proposal={proposal}
          evolutionReplayResult={evolutionReplayResult}
          evolutionReplayBusy={evolutionReplayBusy}
          directEvolutionRunLoadingId={directEvolutionRunLoadingId}
          onReplay={replayActiveEvolution}
        />
      </DetailSheet>
    </RoutePageShell>
  );
}
