import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { AgentListing, AgentProofRun } from "../api";
import {
  AgentProofWorkbench,
  canShareProofDrop,
  proofDropPublicUrl,
} from "./AgentProofWorkbench";

const proof: AgentProofRun = {
  id: 42,
  agent_name: "report agent",
  skill_name: "build/report",
  grant_id: "grant-1",
  status: "passed",
  badge: "verified",
  summary: "private result excerpt",
  error: null,
  args_preview: { prompt: "private input" },
  result: { answer: "private output" },
  events: [{ kind: "private event" }],
  file_ops: [{ op: "create", path: "private/file.txt", size: 12 }],
  events_count: 1,
  file_ops_count: 1,
  card_hash: "card-hash",
  repo_url: null,
  head_sha: "head-sha",
  image: null,
  agent_url: null,
  elapsed_ms: 840,
  created_at: "2026-07-11T12:00:00Z",
  started_at: "2026-07-11T12:00:00Z",
  completed_at: "2026-07-11T12:00:00Z",
};

function agent(isPublic: boolean): AgentListing {
  return {
    id: 7,
    name: proof.agent_name,
    description: "Builds reports",
    version: "1.0.0",
    image: "registry/report-agent:latest",
    public: isPublic,
    status: "ready",
    url: "https://report-agent.example",
    card: {
      name: proof.agent_name,
      description: "Builds reports",
      version: "1.0.0",
      skills: [
        {
          name: proof.skill_name,
          description: "Build a report",
          tags: [],
          input_schema: { type: "object", properties: {} },
        },
      ],
    },
    created_at: "2026-07-10T12:00:00Z",
  };
}

describe("ProofDrop sharing", () => {
  it("offers ProofDrop only for a passed proof on a public agent", () => {
    vi.stubEnv("VITE_PUBLIC_SITE_URL", "https://proofs.example.com");
    const publicHtml = renderWorkbench(
      <AgentProofWorkbench
        agent={agent(true)}
        proofs={[proof]}
        selectedProofId={String(proof.id)}
        onProofCreated={() => undefined}
      />,
    );
    const privateHtml = renderWorkbench(
      <AgentProofWorkbench
        agent={agent(false)}
        proofs={[proof]}
        selectedProofId={String(proof.id)}
        onProofCreated={() => undefined}
      />,
    );

    expect(publicHtml.match(/Share ProofDrop/g)).toHaveLength(1);
    expect(privateHtml).not.toContain("Share ProofDrop");
    expect(canShareProofDrop(true, "passed", "https://proofs.example.com")).toBe(true);
    expect(canShareProofDrop(true, "passed", "")).toBe(false);
    expect(canShareProofDrop(true, "failed", "https://proofs.example.com")).toBe(false);
    expect(canShareProofDrop(false, "passed", "https://proofs.example.com")).toBe(false);
  });

  it("builds an encoded public URL without leaking proof payload data", () => {
    const url = proofDropPublicUrl(proof.agent_name, proof.id, "https://proofs.example.com/");

    expect(url).toBe("https://proofs.example.com/p/report%20agent/42");
    expect(url).not.toContain("private");
  });
});

describe("first proof run", () => {
  it("hands an agent with no proofs the button that makes one", () => {
    const html = renderWorkbench(
      <AgentProofWorkbench agent={agent(true)} proofs={[]} onProofCreated={() => undefined} />,
    );

    expect(html).toContain("No proof run yet");
    expect(html).toContain("Run the first proof");
    expect(html).not.toContain("Proof history");
  });

  it("promises only what a proof run delivers, not a signed receipt", () => {
    // POST /v1/me/agent-proofs/{agent}/run invokes the agent directly, bypassing
    // the sealing gateway: it records a pass/fail run with events and file ops
    // and no signed token, so nothing here may offer one to copy or verify.
    const html = renderWorkbench(
      <AgentProofWorkbench agent={agent(true)} proofs={[]} onProofCreated={() => undefined} />,
    );

    expect(html).toContain("recorded as a pass or fail");
    expect(html).not.toContain("sealed into a signed receipt");
    expect(html).not.toContain("signed receipt");
    expect(html).not.toContain("open, copy, and verify");
    expect(html).not.toContain("tools below");
  });
});

function renderWorkbench(node: JSX.Element): string {
  return renderToStaticMarkup(
    <StaticRouter location={`/my-agents/${encodeURIComponent(proof.agent_name)}/proofs/${proof.id}`}>
      {node}
    </StaticRouter>,
  );
}
