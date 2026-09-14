import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { SubagentReceipt } from "../api";
import { ReceiptEvidence } from "./ReceiptEvidence";

function utf8(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function b64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const PAYLOAD = {
  receipt_id: "4b4d56883bb4d526",
  schema_version: 1,
  agent_name: "research-agent",
  agent_version: "",
  caller: "user:42",
  task_id: "",
  skill_name: "ask",
  input_hash: "ad6b174eb01357d875c98f53aef35ca9c96437ed54de7974b507a5221563a435",
  input_preview: "say hello",
  grant_ids: [],
  file_ops: {
    reads: 0,
    writes: 0,
    bytes_read: 0,
    bytes_written: 0,
    read_paths_preview: [],
    write_paths_preview: [],
  },
  tool_calls: [],
  artifacts: [],
  handoffs: [],
  status: "ok",
  error_type: "",
  result_preview: "hello",
  eval_score: null,
  reviewer: "",
  started_at: 1717439700,
  ended_at: 1717439703,
  elapsed_ms: 3000,
  nonce: "d6290a0f68fcbca4",
};

const TOKEN = `${b64url(utf8(JSON.stringify(PAYLOAD)))}.${b64url(utf8("sig"))}`;

function artifact(overrides: Partial<SubagentReceipt> = {}): SubagentReceipt {
  return {
    id: "4b4d56883bb4d526",
    kind: "receipt",
    label: "Execution receipt",
    event_id: 9,
    receipt_id: "4b4d56883bb4d526",
    agent_name: "research-agent",
    skill_name: "ask",
    task_id: null,
    status: "ok",
    signed_token: TOKEN,
    payload: { kind: "receipt_sealed" },
    created_at: "2026-07-11T12:00:00Z",
    ...overrides,
  };
}

function render(receipts: SubagentReceipt[]): string {
  return renderToStaticMarkup(<ReceiptEvidence receipts={receipts} />);
}

describe("ReceiptEvidence", () => {
  it("decodes the signed token into the documented field groups", () => {
    const html = render([artifact()]);

    for (const group of ["identity", "what ran", "authority", "effects", "outcome", "timing"]) {
      expect(html).toContain(group);
    }
    expect(html).toContain("4b4d56883bb4d526");
    expect(html).toContain("research-agent");
    expect(html).toContain("2024-06-03T18:35:00Z");
    expect(html).toContain("3000ms");
  });

  it("claims nothing about the signature until it has been checked", () => {
    const html = render([artifact()]);

    expect(html).toContain("unverified");
    expect(html).toContain("Check signature");
    expect(html).not.toContain("signature valid");
    expect(html).not.toContain("Ed25519 signature valid");
  });

  it("offers the token itself, since the CLI verifies a raw token offline", () => {
    expect(render([artifact()])).toContain("Copy token");
  });

  it("names the fields the hosted observer left empty instead of blanking rows", () => {
    const html = render([artifact()]);

    expect(html).toContain("file ops, tool calls, artifacts, handoffs");
    expect(html).toContain("eval score, reviewer");
    expect(html).not.toContain("0 read / 0 written");
  });

  it("does not promise a redaction the token format does not perform", () => {
    // "say hello" / "hello" are both under the 240-character limit, so the
    // previews in this token ARE the caller's input and the agent's result.
    const html = render([artifact()]);

    expect(html).toContain("The complete input, verbatim");
    expect(html).toContain("The complete result, verbatim");
    expect(html).not.toContain("never the full input");
    expect(html).not.toContain("First 240 characters");
    expect(html).not.toContain("cannot reconstruct one you do not");
  });

  it("ships the non-redaction warning with the copy affordance", () => {
    const html = render([artifact()]);
    const copyAt = html.indexOf("Copy token");
    const warnAt = html.indexOf("A receipt token is not redacted");

    expect(copyAt).toBeGreaterThan(-1);
    expect(warnAt).toBeGreaterThan(copyAt);
    expect(html).toContain("Check what is in yours before you forward it");
  });

  it("warns on the replay artifact's copy button too", () => {
    const html = render([
      artifact({ id: "session-1", kind: "replay", label: "Replay session", receipt_id: null }),
    ]);
    expect(html).toContain("Copy token");
    expect(html).toContain("not redacted");
  });

  it("cannot be made to draw its own verdict line from an unverified payload", () => {
    const forged = {
      ...PAYLOAD,
      agent_name: `acme${String.fromCharCode(10)}signature valid · key from /v1/public/receipt-keys`,
    };
    const html = render([
      artifact({ signed_token: `${b64url(utf8(JSON.stringify(forged)))}.${b64url(utf8("sig"))}` }),
    ]);

    expect(html).toContain("acme signature valid");
    expect(html).not.toContain(`acme${String.fromCharCode(10)}signature valid`);
    expect(html).toContain("unverified");
  });

  it("says so plainly when a run event carried no token to check", () => {
    const html = render([artifact({ signed_token: null })]);

    expect(html).toContain("nothing here to check");
    expect(html).toContain("a2a receipt verify 4b4d56883bb4d526 --agent research-agent");
    expect(html).not.toContain("Check signature");
  });

  it("surfaces a malformed token as a failure, not as a receipt", () => {
    const html = render([artifact({ signed_token: "garbage" })]);

    expect(html).toContain("malformed receipt token");
    expect(html).not.toContain("what ran");
  });

  it("does not decode a replay session token as an execution receipt", () => {
    const html = render([
      artifact({ id: "session-1", kind: "replay", label: "Replay session", receipt_id: null }),
    ]);

    expect(html).toContain("Replay session");
    expect(html).toContain("nothing here is presented as verified");
    expect(html).not.toContain("what ran");
  });
});
