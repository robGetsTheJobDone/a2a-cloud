import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";
import { DashboardSectionCacheProvider } from "../../components/DashboardSectionCache";
import { RuntimePage } from "./RuntimePage";

function renderRuntime(path = "/runtime") {
  return renderToStaticMarkup(
    <StaticRouter location={path}>
      <DashboardSectionCacheProvider>
        <RuntimePage />
      </DashboardSectionCacheProvider>
    </StaticRouter>,
  );
}

function textFromMarkup(markup: string) {
  return markup
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();
}

describe("RuntimePage", () => {
  it("renders the single posture bar with view nav before runtime data loads", () => {
    const markup = renderRuntime();
    const text = textFromMarkup(markup);

    // Mandate D: one compact posture strip replaces the old banner header.
    expect(markup).toContain("Admin · Runtime");
    // Mandate A: the runtime view nav is always present and labelled.
    expect(markup).toContain('aria-label="Runtime views"');
    expect(text).toContain("Timeline");
    expect(text).toContain("Policy");
    // Active state view title + loading body from the keep-alive ControlRoom.
    expect(text).toContain("Overview");
    expect(markup).toContain("Loading controls");
  });
});
