import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  getTrialRoom,
  listAgents,
  listTrialRooms,
  type TrialRoom,
} from "../../api";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "../DashboardSectionCache";
import {
  decodeRouteSegment,
  normalizeTrialRoomViewId,
  type TrialRoomViewId,
} from "../../navigation";
import {
  mergeTrialRoomList,
  summarizeRooms,
  summarizeTrialAgentReadiness,
} from "../trialRoomUtils";
import {
  formatTrialAgentRefreshMessage,
  normalizeTrialRunDetailSection,
  type TrialRunDetailSectionId,
  type TrialRoomsResource,
} from "./helpers";

/**
 * useTrialRoomsData — owns all data loading, route resolution, and mutation
 * plumbing for the Trial Rooms surface. Pure extraction of the original
 * TrialRooms() body state; behavior is unchanged.
 */
// Section-cache keys for hoisted Trial Rooms selection state. Keeping selection
// in the section cache (mandate B) means navigating between rooms/runs never
// remounts the list or refetches — it only flips local state. Deep-links still
// hydrate this state from the URL on first mount (see hydration effect below).
const TRIAL_SELECTION_KEYS = {
  roomSlug: "operate.trial-rooms.selected-room-slug",
  creating: "operate.trial-rooms.creating",
  view: "operate.trial-rooms.active-view",
  runId: "operate.trial-rooms.selected-run-id",
  runSection: "operate.trial-rooms.active-run-section",
} as const;

export function useTrialRoomsData() {
  const navigate = useNavigate();
  const {
    roomSlug: encodedRoomSlug,
    view: routeView,
    runId: encodedRunId,
    section: routeRunSection,
  } = useParams<{
    roomSlug?: string;
    view?: string;
    runId?: string;
    section?: string;
  }>();

  // --- Hoisted selection state (mandate B: no reload/refetch on nav) ---------
  const [selectedRoomSlug, setSelectedRoomSlug] =
    useDashboardSectionState<string | null>(TRIAL_SELECTION_KEYS.roomSlug, null);
  const [creating, setCreating] = useDashboardSectionState<boolean>(
    TRIAL_SELECTION_KEYS.creating,
    false,
  );
  const [activeView, setActiveView] = useDashboardSectionState<TrialRoomViewId>(
    TRIAL_SELECTION_KEYS.view,
    "overview",
  );
  const [selectedRunId, setSelectedRunId] = useDashboardSectionState<string | null>(
    TRIAL_SELECTION_KEYS.runId,
    null,
  );
  const [activeRunSection, setActiveRunSection] =
    useDashboardSectionState<TrialRunDetailSectionId>(
      TRIAL_SELECTION_KEYS.runSection,
      "overview",
    );

  // --- Deep-link hydration (mandate C: map URL -> state on mount, no redirect)
  // Runs once. Existing /trials/:slug[/runs/:id[/:section]][/:view] URLs seed
  // the hoisted state instead of being treated as the live source of truth, so
  // the list never unmounts and subsequent selection changes are pure state.
  const hydratedRef = useRef(false);
  if (!hydratedRef.current) {
    hydratedRef.current = true;
    const initialRoomSlug = decodeRouteSegment(encodedRoomSlug);
    if (initialRoomSlug === "new") {
      setCreating(true);
    } else if (initialRoomSlug) {
      setSelectedRoomSlug(initialRoomSlug);
    }
    const initialRunId = decodeRouteSegment(encodedRunId);
    if (initialRunId) setSelectedRunId(initialRunId);
    if (routeView) setActiveView(normalizeTrialRoomViewId(routeView));
    if (routeRunSection) {
      setActiveRunSection(normalizeTrialRunDetailSection(routeRunSection));
    }
  }

  const routeRoomSlug = creating ? "new" : selectedRoomSlug;
  const routeRunId = selectedRunId;
  const loadTrialRoomsResource = useCallback(async (): Promise<TrialRoomsResource> => {
    const [nextRooms, nextAgents] = await Promise.all([
      listTrialRooms({ limit: 100 }),
      listAgents(),
    ]);
    return {
      rooms: nextRooms,
      agents: nextAgents,
    };
  }, []);
  const {
    data,
    error: loadErr,
    setData: setTrialRoomsData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.operate.trialRooms,
    loadTrialRoomsResource,
  );
  const rooms = data?.rooms ?? null;
  const agents = data?.agents ?? [];
  const err = loadErr;
  const [candidateAgentsRefreshing, setCandidateAgentsRefreshing] = useState(false);
  const [candidateAgentsErr, setCandidateAgentsErr] = useState<string | null>(null);
  const [candidateAgentsMessage, setCandidateAgentsMessage] = useState<string | null>(null);
  const [routeRoomLoadingSlug, setRouteRoomLoadingSlug] = useState<string | null>(null);
  const [routeRoomErr, setRouteRoomErr] = useState<string | null>(null);
  const loadingRouteRoom = Boolean(
    routeRoomSlug && routeRoomLoadingSlug === routeRoomSlug,
  );

  const selected = useMemo(() => {
    if (creating) return null;
    if (!rooms?.length) return null;
    if (routeRoomSlug) {
      return rooms.find((room) => room.slug === routeRoomSlug) || null;
    }
    return rooms[0];
  }, [creating, rooms, routeRoomSlug]);
  const selectedRun = useMemo(() => {
    if (!selected || !routeRunId) return null;
    return selected.runs.find((run) => String(run.id) === routeRunId) || null;
  }, [routeRunId, selected]);

  // Default selection without a route redirect (mandate B/C). When nothing is
  // selected yet, adopt the first room as in-place state instead of navigating.
  useEffect(() => {
    if (creating) return;
    if (!rooms?.length) return;
    if (!selectedRoomSlug) {
      setSelectedRoomSlug(rooms[0].slug);
    }
  }, [creating, rooms, selectedRoomSlug, setSelectedRoomSlug]);

  useEffect(() => {
    if (creating || !routeRoomSlug) {
      setRouteRoomLoadingSlug(null);
      setRouteRoomErr(null);
      return;
    }
    if (rooms === null) return;
    if (rooms.some((room) => room.slug === routeRoomSlug)) {
      setRouteRoomLoadingSlug(null);
      setRouteRoomErr(null);
      return;
    }

    let cancelled = false;
    setRouteRoomLoadingSlug(routeRoomSlug);
    setRouteRoomErr(null);
    void getTrialRoom(routeRoomSlug)
      .then((room) => {
        if (cancelled) return;
        setTrialRoomsData((current) => ({
          rooms: mergeTrialRoomList(current?.rooms || [], room),
          agents: current?.agents || agents,
        }));
      })
      .catch((ex) => {
        if (cancelled) return;
        setRouteRoomErr(ex instanceof Error ? ex.message : String(ex));
      })
      .finally(() => {
        if (!cancelled) setRouteRoomLoadingSlug(null);
      });

    return () => {
      cancelled = true;
    };
  }, [agents, creating, rooms, routeRoomSlug, setTrialRoomsData]);

  const upsertRoom = useCallback(
    (room: TrialRoom) => {
      setTrialRoomsData((current) => {
        return {
          rooms: mergeTrialRoomList(current?.rooms || [], room),
          agents: current?.agents || agents,
        };
      });
      // Select in place (mandate C) instead of a route redirect that would
      // unmount the list. Leaving the create flow returns to the room view.
      setCreating(false);
      if (room.slug !== selectedRoomSlug) setSelectedRoomSlug(room.slug);
    },
    [agents, selectedRoomSlug, setCreating, setSelectedRoomSlug, setTrialRoomsData],
  );

  const refreshCandidateAgents = useCallback(async () => {
    if (candidateAgentsRefreshing) return;
    setCandidateAgentsRefreshing(true);
    setCandidateAgentsErr(null);
    setCandidateAgentsMessage(null);
    try {
      const nextAgents = await listAgents();
      const readiness = summarizeTrialAgentReadiness(nextAgents);
      setTrialRoomsData((current) => ({
        rooms: current?.rooms || rooms || [],
        agents: nextAgents,
      }));
      setCandidateAgentsMessage(formatTrialAgentRefreshMessage(readiness));
    } catch (ex) {
      setCandidateAgentsErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setCandidateAgentsRefreshing(false);
    }
  }, [candidateAgentsRefreshing, rooms, setTrialRoomsData]);

  const totals = summarizeRooms(rooms || []);

  return {
    navigate,
    creating,
    setCreating,
    activeView,
    setActiveView,
    activeRunSection,
    setActiveRunSection,
    selectedRoomSlug,
    setSelectedRoomSlug,
    selectedRunId,
    setSelectedRunId,
    routeRoomSlug,
    routeRunId,
    rooms,
    agents,
    err,
    selected,
    selectedRun,
    loadingRouteRoom,
    routeRoomErr,
    candidateAgentsRefreshing,
    candidateAgentsErr,
    candidateAgentsMessage,
    totals,
    upsertRoom,
    refreshCandidateAgents,
  };
}
