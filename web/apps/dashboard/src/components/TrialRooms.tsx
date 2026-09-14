import { EmptyState, LoadingState, ToolbarButton } from "./DashboardChrome";
import { DetailSheet, ListDetailLayout } from "./ListDetailLayout";
import { useTrialRoomsData } from "./trial-rooms/useTrialRoomsData";
import { TrialRoomList } from "./trial-rooms/TrialRoomList";
import { TrialRoomDetail } from "./trial-rooms/TrialRoomDetail";
import { TrialRunDetailView } from "./trial-rooms/TrialRunDetail";
import { NewTrialRoomPage } from "./trial-rooms/NewTrialRoom";
import type { TrialRunDetailSectionId } from "./trial-rooms/helpers";

/**
 * TrialRooms — thin composer for the Agent Trials surface.
 *
 * Redesigned to the list/detail shell (mandates A–E): the room list is a
 * persistent left rail (ListDetailLayout, never unmounted), room selection is
 * hoisted state (no route reload/refetch), per-run detail opens as an in-place
 * right DetailSheet, and the create flow opens as a right-side sheet — never a
 * full-page route redirect that would unmount the list. Deep-links still
 * hydrate selection on mount via useTrialRoomsData. Public TrialRooms() export
 * stays stable for its sole importer, pages/work/TrialsPage.tsx.
 */
export function TrialRooms() {
  const {
    creating,
    setCreating,
    activeView,
    setActiveView,
    activeRunSection,
    setActiveRunSection,
    routeRunId,
    rooms,
    agents,
    err,
    selected,
    selectedRun,
    setSelectedRoomSlug,
    setSelectedRunId,
    loadingRouteRoom,
    routeRoomErr,
    candidateAgentsRefreshing,
    candidateAgentsErr,
    candidateAgentsMessage,
    totals,
    upsertRoom,
    refreshCandidateAgents,
  } = useTrialRoomsData();

  const openCreate = () => setCreating(true);
  const closeCreate = () => setCreating(false);
  const selectRoom = (slug: string) => {
    setCreating(false);
    setSelectedRunId(null);
    setSelectedRoomSlug(slug);
  };
  const openRun = (runId: number | string, section: TrialRunDetailSectionId) => {
    setActiveRunSection(section);
    setSelectedRunId(String(runId));
  };
  const closeRun = () => setSelectedRunId(null);

  const runOpen = Boolean(selected && routeRunId);

  const detail = selected ? (
    <TrialRoomDetail
      room={selected}
      agents={agents}
      activeView={activeView}
      onViewChange={setActiveView}
      onOpenRun={openRun}
      onRoomChange={upsertRoom}
      onRefreshAgents={refreshCandidateAgents}
      agentRefreshBusy={candidateAgentsRefreshing}
      agentRefreshErr={candidateAgentsErr}
      agentRefreshMessage={candidateAgentsMessage}
    />
  ) : null;

  const empty =
    rooms === null || loadingRouteRoom ? (
      <LoadingState label="Loading trial room..." />
    ) : routeRoomErr ? (
      <EmptyState
        title="Trial room not found"
        description={routeRoomErr}
        action={
          <ToolbarButton type="button" variant="primary" onClick={() => setSelectedRoomSlug(null)}>
            Open trial list
          </ToolbarButton>
        }
      />
    ) : (
      <EmptyState
        title="No trial selected"
        description="Create a Trial Room to test agents on real work before you pay for one."
        action={
          <ToolbarButton type="button" variant="primary" onClick={openCreate}>
            New trial
          </ToolbarButton>
        }
      />
    );

  return (
    <>
      <ListDetailLayout
        data-onboarding-target="trials-detail"
        className="mx-auto h-full w-full max-w-7xl"
        listWidth="320px"
        listLabel="Trial rooms"
        list={
          <TrialRoomList
            rooms={rooms}
            selected={selected}
            creating={creating}
            err={err}
            totals={totals}
            onSelect={selectRoom}
            onCreate={openCreate}
          />
        }
        detail={detail}
        empty={empty}
      />

      <DetailSheet
        open={runOpen}
        onClose={closeRun}
        size="lg"
        title={
          selectedRun
            ? `${selectedRun.agent_name}.${selectedRun.skill_name}`
            : "Run not found"
        }
        description="Trial run evidence, review, and receipt."
      >
        {selected && routeRunId ? (
          <TrialRunDetailView
            room={selected}
            run={selectedRun}
            requestedRunId={routeRunId}
            activeSection={activeRunSection}
            onSectionChange={setActiveRunSection}
            onRoomChange={upsertRoom}
          />
        ) : null}
      </DetailSheet>

      <DetailSheet
        open={creating}
        onClose={closeCreate}
        size="lg"
        title="Create a private proof room"
        description="Agent trials"
      >
        <NewTrialRoomPage onCancel={closeCreate} onCreated={upsertRoom} />
      </DetailSheet>
    </>
  );
}
