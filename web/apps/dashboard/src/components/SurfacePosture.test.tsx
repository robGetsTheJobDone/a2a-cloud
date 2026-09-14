import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DashboardSurfacePosture } from "./SurfacePosture";

describe("DashboardSurfacePosture", () => {
  it("renders eyebrow, title, status pill, metrics, and actions", () => {
    const html = renderToStaticMarkup(
      <DashboardSurfacePosture
        eyebrow="fleet"
        title="Agent fleet posture"
        status={{ label: "healthy", tone: "live" }}
        metrics={[
          { label: "live", value: "12" },
          { label: "failures", value: "0", tone: "danger" },
        ]}
        actions={<button type="button">Queue updates</button>}
      />,
    );

    expect(html).toContain("fleet");
    expect(html).toContain("Agent fleet posture");
    expect(html).toContain("healthy");
    expect(html).toContain("live");
    expect(html).toContain("failures");
    expect(html).toContain("Queue updates");
    // danger-toned metric uses the signal accent ramp, not raw colors.
    expect(html).toContain("text-signal-danger");
  });

  it("renders a minimal title-only strip without optional slots", () => {
    const html = renderToStaticMarkup(
      <DashboardSurfacePosture title="Runtime" />,
    );

    expect(html).toContain("Runtime");
    // No metrics description list when metrics are omitted.
    expect(html).not.toContain("<dl");
    // No actions slot when actions are omitted.
    expect(html).not.toContain("ml-auto");
  });
});
