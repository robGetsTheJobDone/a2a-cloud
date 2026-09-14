import { renderToStaticMarkup } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";
import { DashboardSectionCacheProvider } from "../../components/DashboardSectionCache";
import { Activity } from "../../components/Activity";

function renderActivityMarkup(location = "/activity") {
  // Mount Activity under the same `:jobId/:section` route patterns the keep-alive
  // host registers, so `useParams` resolves deep-links exactly as in the app.
  return renderToStaticMarkup(
    <StaticRouter location={location}>
      <DashboardSectionCacheProvider>
        <Routes>
          <Route path="/activity" element={<Activity />}>
            <Route path=":jobId" element={null} />
            <Route path=":jobId/:section" element={null} />
          </Route>
        </Routes>
      </DashboardSectionCacheProvider>
    </StaticRouter>,
  );
}

describe("Activity ledger", () => {
  it("surfaces deep-linked ledger filters on first paint", () => {
    const html = renderActivityMarkup(
      "/activity?agent=billing-agent&source=proof&grant=grant-1&status=failed&q=receipt",
    );

    expect(html).toContain("5 filters active");
    expect(html).toContain("Filtered by");
    expect(html).toContain("billing-agent");
    expect(html).toContain("proof");
    expect(html).toContain("failed");
    expect(html).toContain("grant-1");
    expect(html).toContain("receipt");
    expect(html).toContain("Loading activity...");
  });

  it("keeps the ledger list mounted while no job detail sheet is open", () => {
    const html = renderActivityMarkup("/activity");

    // Mandate E: the list rail is always mounted...
    expect(html).toContain('data-onboarding-target="activity-list"');
    // ...and mandate C: with nothing selected the right-side detail sheet
    // (a placement="right" Dialog) is not rendered, so it never covers the list.
    expect(html).not.toContain('role="dialog"');
  });

  it("opens the selected job as an in-place detail sheet over the kept-alive list (deep-link)", () => {
    // Mandate C: a deep-linked /activity/:jobId hydrates the selection on first
    // paint and renders the detail as a right-side sheet (role=dialog) layered
    // OVER the ledger list, which stays mounted (mandate B/E) rather than being
    // replaced by a full-page route redirect.
    const html = renderActivityMarkup("/activity/job-abc123/events");

    // The list rail is still present underneath the sheet (never unmounted).
    expect(html).toContain('data-onboarding-target="activity-list"');
    // The detail surfaces as an in-place sheet, not a routed full page.
    expect(html).toContain('role="dialog"');
    expect(html).toContain('data-onboarding-target="activity-detail"');
    // The hydrated selection shows the deep-linked job id in the sheet header.
    expect(html).toContain("job-abc123");
  });
});
