import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SegmentedButton, SegmentedControl } from "./DashboardChrome";

describe("DashboardChrome segmented controls", () => {
  it("keeps labels intact while the control scrolls on narrow screens", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl aria-label="Runtime views">
        <SegmentedButton selected>Overview</SegmentedButton>
        <SegmentedButton>Timeline</SegmentedButton>
        <SegmentedButton>Policy</SegmentedButton>
        <SegmentedButton>Memory</SegmentedButton>
        <SegmentedButton>Dispatch</SegmentedButton>
        <SegmentedButton>Artifacts</SegmentedButton>
      </SegmentedControl>,
    );

    expect(html).toContain("overflow-x-auto");
    expect(html).toContain("shrink-0");
    expect(html).toContain("whitespace-nowrap");
    expect(html).not.toContain("min-w-0");
    expect(html).toContain('aria-label="Runtime views"');
  });
});
