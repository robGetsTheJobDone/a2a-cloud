import { FullHeightRouteFrame } from "../../components/DashboardChrome";
import { TrialRooms } from "../../components/TrialRooms";

/**
 * TrialsPage — full-height host for the Agent Trials surface.
 *
 * Keep-alive audit (mandate E): no PersistentRoutePanel wrapper is needed here.
 * Page-level keep-alive is provided by the DashboardRoutes RoutePane host, which
 * mounts each visited route once and toggles it via `hidden` instead of
 * unmounting on navigation. In-page sub-views (overview/run/comparison/receipts,
 * the run detail, and the create flow) are now selected from hoisted state in
 * useTrialRoomsData (selectedRoomSlug/selectedRunId/activeView/creating) and
 * rendered conditionally inside TrialRooms — so navigating between rooms or runs
 * never reloads data or unmounts the list (mandates B/C). The `:roomSlug`,
 * `:runId`, `:view`, and `:section` route params still deep-link: the hook
 * hydrates that state from the URL on first mount, so existing URLs do not 404.
 * The full-height frame is supplied here because the trials route is configured
 * `layout: "full-height"` (DashboardRoutes skips ScrollRouteFrame for it).
 */
export function TrialsPage() {
  return (
    <FullHeightRouteFrame>
      <TrialRooms />
    </FullHeightRouteFrame>
  );
}
