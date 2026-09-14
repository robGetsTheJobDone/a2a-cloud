import { describe, expect, it } from "vitest";
import {
  cleanReceiptText,
  groupReceiptFields,
  isTruncatedPreview,
  parseReceiptToken,
  populatedObserverFields,
  previewNote,
  receiptVerificationBadge,
  receiptVerificationLabel,
  receiptVerificationTone,
  receiptVerifyCommand,
  unobservedReceiptFields,
  PREVIEW_LIMIT,
  RECEIPT_TOKEN_DISCLOSURE,
  type ReceiptFieldGroup,
} from "./receiptToken";

function utf8(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function b64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The payload shape from /concepts/receipts, as a hosted gateway seals it. */
const HOSTED_PAYLOAD = {
  receipt_id: "4b4d56883bb4d526",
  schema_version: 1,
  agent_name: "research-agent",
  agent_version: "",
  caller: "user:42",
  task_id: "",
  skill_name: "ask",
  input_hash: "ad6b174eb01357d875c98f53aef35ca9c96437ed54de7974b507a5221563a435",
  input_preview: '{"prompt": "say hello"}',
  grant_ids: [] as string[],
  file_ops: {
    reads: 0,
    writes: 0,
    bytes_read: 0,
    bytes_written: 0,
    read_paths_preview: [] as string[],
    write_paths_preview: [] as string[],
  },
  tool_calls: [] as unknown[],
  artifacts: [] as unknown[],
  handoffs: [] as unknown[],
  status: "ok",
  error_type: "",
  result_preview: "hello",
  eval_score: null as number | null,
  reviewer: "",
  started_at: 1717439700,
  ended_at: 1717439703,
  elapsed_ms: 3000,
  nonce: "d6290a0f68fcbca4",
};

function token(payload: unknown, signature = b64url(utf8("sig"))): string {
  return `${b64url(utf8(JSON.stringify(payload)))}.${signature}`;
}

function group(groups: ReceiptFieldGroup[], id: string): ReceiptFieldGroup {
  const found = groups.find((entry) => entry.id === id);
  if (!found) throw new Error(`missing group ${id}`);
  return found;
}

function receiptOf(payload: unknown) {
  const result = parseReceiptToken(token(payload));
  if (!result.ok) throw new Error(`expected a parsable token, got: ${result.error}`);
  return result;
}

describe("parseReceiptToken", () => {
  it("rejects a token with no signature segment", () => {
    expect(parseReceiptToken("")).toEqual({ ok: false, error: "malformed receipt token" });
    expect(parseReceiptToken(null)).toEqual({ ok: false, error: "malformed receipt token" });
    expect(parseReceiptToken("eyJhIjoxfQ")).toEqual({
      ok: false,
      error: "malformed receipt token",
    });
  });

  it("rejects non-canonical base64url before looking at the payload", () => {
    // "AR" decodes to the same byte as "AQ" but leaves non-zero trailing bits.
    expect(parseReceiptToken("AR.c2ln")).toEqual({
      ok: false,
      error: "receipt decode failed: non-canonical base64url encoding",
    });
    // The standard-base64 alphabet is not base64url.
    expect(parseReceiptToken("a+b/c.c2ln")).toEqual({
      ok: false,
      error: "receipt decode failed: non-canonical base64url encoding",
    });
  });

  it("rejects a well-encoded payload that is not a receipt", () => {
    const result = parseReceiptToken(token({ hello: "world" }));
    expect(result).toEqual({
      ok: false,
      error: 'receipt payload invalid: missing or non-string field "receipt_id"',
    });
  });

  it("names the required field a truncated payload is missing", () => {
    const withoutAgent: Record<string, unknown> = { ...HOSTED_PAYLOAD };
    delete withoutAgent.agent_name;
    expect(parseReceiptToken(token(withoutAgent))).toEqual({
      ok: false,
      error: 'receipt payload invalid: missing or non-string field "agent_name"',
    });
  });

  it("hands back the exact signed bytes, not a re-serialization", () => {
    const json = JSON.stringify(HOSTED_PAYLOAD);
    const result = receiptOf(HOSTED_PAYLOAD);
    expect(new TextDecoder().decode(result.payloadBytes)).toBe(json);
    expect(new TextDecoder().decode(result.signature)).toBe("sig");
    expect(result.receipt.receipt_id).toBe("4b4d56883bb4d526");
    expect(result.receipt.elapsed_ms).toBe(3000);
    expect(result.receipt.eval_score).toBeNull();
  });
});

describe("populated vs unobserved fields", () => {
  it("treats every observer-optional field on a hosted receipt as unfilled", () => {
    const { receipt } = receiptOf(HOSTED_PAYLOAD);
    expect(populatedObserverFields(receipt)).toEqual([]);
    expect(unobservedReceiptFields(receipt)).toEqual([
      "file_ops",
      "tool_calls",
      "artifacts",
      "handoffs",
      "eval_score",
      "reviewer",
    ]);
  });

  it("counts a zero-count file_ops carrying a path preview as observed", () => {
    const { receipt } = receiptOf({
      ...HOSTED_PAYLOAD,
      file_ops: { ...HOSTED_PAYLOAD.file_ops, read_paths_preview: ["src/app.ts"] },
    });
    expect(populatedObserverFields(receipt)).toEqual(["file_ops"]);
  });

  it("counts an eval_score of 0 as observed, not as absent", () => {
    const { receipt } = receiptOf({ ...HOSTED_PAYLOAD, eval_score: 0 });
    expect(populatedObserverFields(receipt)).toEqual(["eval_score"]);
  });
});

describe("groupReceiptFields", () => {
  it("projects onto the same six groups the docs and the CLI use", () => {
    const { receipt } = receiptOf(HOSTED_PAYLOAD);
    expect(groupReceiptFields(receipt).map((entry) => entry.id)).toEqual([
      "identity",
      "call",
      "authority",
      "effects",
      "outcome",
      "timing",
    ]);
  });

  it("omits unfilled observer fields and names them instead of blanking rows", () => {
    const groups = groupReceiptFields(receiptOf(HOSTED_PAYLOAD).receipt);

    const effects = group(groups, "effects");
    expect(effects.rows).toEqual([]);
    expect(effects.unobserved).toContain("file ops, tool calls, artifacts, handoffs");
    expect(effects.unobserved).toContain("not pending data");

    const outcome = group(groups, "outcome");
    expect(outcome.rows.map((row) => row.label)).toEqual(["status", "error", "result"]);
    expect(outcome.unobserved).toContain("eval score, reviewer");
  });

  it("renders the observer fields a richer sealer did fill, with no note", () => {
    const groups = groupReceiptFields(
      receiptOf({
        ...HOSTED_PAYLOAD,
        file_ops: {
          reads: 2,
          writes: 1,
          bytes_read: 40,
          bytes_written: 12,
          read_paths_preview: ["a.txt"],
          write_paths_preview: ["b.txt"],
        },
        tool_calls: [{ name: "search", args_hash: "abc", status: "ok", elapsed_ms: 12 }],
        artifacts: [{ path: "out/report.md", mime_type: "text/markdown", bytes: 90 }],
        handoffs: [
          { callee: "writer", skill: "draft", grant_id: "g1", elapsed_ms: 5, status: "ok" },
        ],
        eval_score: 0.75,
        reviewer: "judge-agent",
      }).receipt,
    );

    const effects = group(groups, "effects");
    expect(effects.rows.map((row) => row.label)).toEqual([
      "files",
      "tools",
      "artifacts",
      "handoffs",
    ]);
    expect(effects.rows[0].value).toBe("2 read / 1 written (40 B in, 12 B out)");
    expect(effects.unobserved).toBeUndefined();

    const outcome = group(groups, "outcome");
    expect(outcome.rows.map((row) => row.label)).toEqual([
      "status",
      "error",
      "result",
      "eval score",
      "reviewer",
    ]);
    expect(outcome.unobserved).toBeUndefined();
  });

  it("never presents a hash as the real input", () => {
    const call = group(groupReceiptFields(receiptOf(HOSTED_PAYLOAD).receipt), "call");

    const hash = call.rows.find((row) => row.label === "input hash");
    expect(hash?.value).toBe(HOSTED_PAYLOAD.input_hash);
    expect(hash?.note).toContain("does not reveal the input");
  });

  it("calls a short preview the complete input, because that is what it is", () => {
    // `_preview` truncates only above the limit, so a 23-character input is
    // sealed verbatim. Saying "first 240 characters" here would be a privacy
    // assurance the format does not make.
    const call = group(groupReceiptFields(receiptOf(HOSTED_PAYLOAD).receipt), "call");
    const input = call.rows.find((row) => row.label === "input");

    expect(input?.value).toBe(HOSTED_PAYLOAD.input_preview);
    expect(input?.note).toBe(
      "The complete input, verbatim: it was under the 240-character preview limit.",
    );
    expect(input?.note).not.toContain("First 240 characters");
    expect(input?.note).not.toContain("not the input");
  });

  it("says a long preview was truncated, and only then", () => {
    // Exactly what `_preview` emits above the limit: limit-1 characters + "…".
    const truncated = "x".repeat(PREVIEW_LIMIT - 1) + "…";
    const groups = groupReceiptFields(
      receiptOf({
        ...HOSTED_PAYLOAD,
        input_preview: truncated,
        result_preview: truncated,
      }).receipt,
    );

    expect(isTruncatedPreview(truncated)).toBe(true);
    expect(group(groups, "call").rows.find((row) => row.label === "input")?.note).toBe(
      "Truncated to 240 characters — the rest of the input is not in the receipt.",
    );
    expect(group(groups, "outcome").rows.find((row) => row.label === "result")?.note).toBe(
      "Truncated to 240 characters — the rest of the result is not in the receipt.",
    );
  });

  it("does not mistake a short value that merely ends in an ellipsis for a truncation", () => {
    expect(isTruncatedPreview("to be continued…")).toBe(false);
    expect(previewNote("result", "to be continued…")).toContain("The complete result");
    // Python counts code points, so an emoji-bearing preview at the limit still
    // reads as truncated rather than as a verbatim result.
    expect(isTruncatedPreview("🙂".repeat(PREVIEW_LIMIT - 1) + "…")).toBe(true);
  });

  it("does not claim a preview at all when the sealer wrote none", () => {
    expect(previewNote("input", "")).toBe("No input preview was sealed into this receipt.");
  });

  it("ships the docs' non-redaction warning as copy the UI can render", () => {
    expect(RECEIPT_TOKEN_DISCLOSURE).toContain("not redacted");
    expect(RECEIPT_TOKEN_DISCLOSURE).toContain("base64-decode");
    expect(RECEIPT_TOKEN_DISCLOSURE).toContain("before you forward it");
  });

  it("dims legitimately empty identity fields instead of hiding them", () => {
    const identity = group(groupReceiptFields(receiptOf(HOSTED_PAYLOAD).receipt), "identity");
    expect(identity.rows.map((row) => [row.label, row.value, row.empty])).toEqual([
      ["receipt", "4b4d56883bb4d526", false],
      ["agent", "research-agent", false],
      ["caller", "user:42", false],
      ["task", "-", true],
    ]);
  });

  it("formats timing from unix seconds", () => {
    const timing = group(groupReceiptFields(receiptOf(HOSTED_PAYLOAD).receipt), "timing");
    expect(timing.rows.map((row) => row.value)).toEqual([
      "2024-06-03T18:35:00Z",
      "2024-06-03T18:35:03Z",
      "3000ms",
    ]);
  });

  it("shows an unsealed timestamp as absent rather than the unix epoch", () => {
    const timing = group(
      groupReceiptFields(receiptOf({ ...HOSTED_PAYLOAD, started_at: 0 }).receipt),
      "timing",
    );
    expect(timing.rows[0]).toMatchObject({ label: "started", value: "-", empty: true });
  });
});

describe("untrusted payload strings", () => {
  const LF = String.fromCharCode(10);
  const DEL = String.fromCharCode(0x7f);
  const NEL = String.fromCharCode(0x85);

  it("collapses control characters so a payload cannot draw its own rows", () => {
    // A forger controls every string in an unverified payload, and the field
    // cells are whitespace-pre-wrap: newlines would let the payload paint an
    // extra line that reads like a verdict.
    const forged = `acme${LF}${LF}signature valid · key from /v1/public/receipt-keys`;
    const { receipt } = receiptOf({ ...HOSTED_PAYLOAD, agent_name: forged });

    expect(receipt.agent_name).not.toContain(LF);
    expect(receipt.agent_name).toBe(
      "acme  signature valid · key from /v1/public/receipt-keys",
    );
  });

  it("cleans every payload-derived string, including list and nested members", () => {
    const { receipt } = receiptOf({
      ...HOSTED_PAYLOAD,
      caller: `user${LF}root`,
      grant_ids: [`grant-1${LF}grant-forged`],
      result_preview: `ok${DEL}${NEL}done`,
      tool_calls: [{ name: `search${LF}x`, args_hash: "abc", status: "ok", elapsed_ms: 1 }],
    });

    expect(receipt.caller).toBe("user root");
    expect(receipt.grant_ids).toEqual(["grant-1 grant-forged"]);
    expect(receipt.result_preview).toBe("ok  done");
    expect(receipt.tool_calls[0].name).toBe("search x");
  });

  it("still hands the signature the untouched bytes", () => {
    const payload = { ...HOSTED_PAYLOAD, agent_name: `acme${LF}forged` };
    const parsed = receiptOf(payload);

    expect(new TextDecoder().decode(parsed.payloadBytes)).toBe(JSON.stringify(payload));
    expect(parsed.receipt.agent_name).toBe("acme forged");
  });

  it("keeps the line breaks the view builds itself", () => {
    const authority = group(
      groupReceiptFields(receiptOf({ ...HOSTED_PAYLOAD, grant_ids: ["g1", "g2"] }).receipt),
      "authority",
    );
    expect(authority.rows[0].value).toBe(`g1${LF}g2`);
  });

  it("leaves ordinary text, including the truncation ellipsis, alone", () => {
    expect(cleanReceiptText('{"prompt": "say hello"} …')).toBe('{"prompt": "say hello"} …');
    expect(cleanReceiptText("🙂 ok")).toBe("🙂 ok");
  });
});

describe("verification presentation", () => {
  it("produces the runnable offline command", () => {
    expect(receiptVerifyCommand("  abc.def  ")).toBe("a2a receipt verify --token 'abc.def'");
  });

  it("never labels an unchecked receipt as verified", () => {
    expect(receiptVerificationBadge({ state: "unchecked" })).toBe("unverified");
    expect(receiptVerificationLabel({ state: "unchecked" })).toContain("unverified claims");
    expect(receiptVerificationTone({ state: "unchecked" })).toBe("neutral");
  });

  it("names the key a valid signature was checked against", () => {
    const label = receiptVerificationLabel({ state: "valid", kid: "56475aa75463474c" });
    expect(label).toContain("56475aa75463474c");
    expect(label).toContain("/v1/public/receipt-keys");
    expect(receiptVerificationTone({ state: "valid", kid: "x" })).toBe("emerald");
    expect(receiptVerificationBadge({ state: "valid", kid: "x" })).toBe("signature valid");
  });

  it("tells the owner not to trust a failed receipt", () => {
    const failed = { state: "invalid", detail: "receipt signature mismatch" } as const;
    expect(receiptVerificationLabel(failed)).toContain("Do not trust");
    expect(receiptVerificationTone(failed)).toBe("red");
    expect(receiptVerificationBadge(failed)).toBe("signature failed");
  });

  it("keeps 'cannot check here' visually distinct from both pass and fail", () => {
    const unavailable = {
      state: "unavailable",
      reason: "ed25519-unsupported",
      detail: "This browser cannot check Ed25519 signatures: NotSupportedError",
    } as const;
    expect(receiptVerificationLabel(unavailable)).toContain("Cannot check here");
    expect(receiptVerificationTone(unavailable)).toBe("amber");
    expect(receiptVerificationBadge(unavailable)).toBe("not checkable here");
  });
});
