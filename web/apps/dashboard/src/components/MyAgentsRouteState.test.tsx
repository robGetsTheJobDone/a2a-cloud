import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";
import { AgentDetailRouteState } from "./MyAgents";

function renderRouteState(
  node: JSX.Element,
  location = "/my-agents/app-flow-smoke-20260625t173231449-529287",
) {
  return renderToStaticMarkup(
    <StaticRouter location={location}>
      {node}
    </StaticRouter>,
  );
}

describe("AgentDetailRouteState", () => {
  it("keeps the requested agent route visible while fleet summaries load", () => {
    const html = renderRouteState(
      <AgentDetailRouteState
        agentName="app-flow-smoke-20260625t173231449-529287"
        loading
        error={null}
        onBack={() => undefined}
        onRetry={() => undefined}
        importHref="/my-agents/import?requested_agent=app-flow-smoke-20260625t173231449-529287"
        agents={null}
        search=""
      />,
    );

    expect(html).toContain("my agents/app flow smoke 20260625t173231449 529287");
    expect(html).toContain("Checking owned-agent summaries");
    expect(html).toContain("Refresh lookup");
    expect(html).toContain("Bring this agent");
    expect(html).toContain(
      'href="/my-agents/import?requested_agent=app-flow-smoke-20260625t173231449-529287"',
    );
  });

  it("renders nearby fleet matches without carrying stale requested-agent search", () => {
    const html = renderRouteState(
      <AgentDetailRouteState
        agentName="app-flow-smoke-20260625t173231449-529287"
        loading={false}
        error="404: Not found"
        onBack={() => undefined}
        onRetry={() => undefined}
        importHref="/my-agents/import?requested_agent=app-flow-smoke-20260625t173231449-529287"
        agents={[
          {
            name: "app-flow-smoke-20260625t173231449-529999",
            description: "Production smoke replacement",
            status: "running",
            public: false,
            latest_deployment: { status: "live" },
          },
        ]}
        search="?workspace=ops&requested_agent=stale"
      />,
    );

    expect(html).toContain("Nearby fleet matches");
    expect(html).toContain("app-flow-smoke-20260625t173231449-529999");
    expect(html).toContain("Production smoke replacement");
    expect(html).toContain(
      'href="/my-agents/app-flow-smoke-20260625t173231449-529999?workspace=ops"',
    );
    expect(html).not.toContain("requested_agent=stale");
  });
});
