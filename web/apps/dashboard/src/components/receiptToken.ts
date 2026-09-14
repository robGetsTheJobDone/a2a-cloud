/**
 * Execution receipt tokens, decoded in the browser.
 *
 * The wire format is owned by `a2a_pack.receipts`:
 *
 *     <base64url(receipt.model_dump_json())>.<base64url(ed25519 signature)>
 *
 * Both segments are unpadded base64url and the signature covers the payload
 * bytes exactly as they were serialized, so decoding has to hand those *bytes*
 * back — re-serializing the parsed object would not be the signed message.
 *
 * Everything here is pure: no network, no crypto, no React. The signature check
 * lives in ./receiptVerification, which is imported on demand so the crypto and
 * key-fetch path stays out of the first-paint bundle.
 */

type ReceiptFileOps = {
  reads: number;
  writes: number;
  bytes_read: number;
  bytes_written: number;
  read_paths_preview: string[];
  write_paths_preview: string[];
};

type ReceiptToolCall = {
  name: string;
  args_hash: string;
  status: string;
  elapsed_ms: number;
};

type ReceiptArtifact = {
  path: string;
  mime_type: string;
  bytes: number;
};

type ReceiptHandoff = {
  callee: string;
  skill: string;
  grant_id: string;
  elapsed_ms: number;
  status: string;
};

/** Mirrors `a2a_pack.receipts.ExecutionReceipt` (schema_version 1). */
export type ExecutionReceiptPayload = {
  receipt_id: string;
  schema_version: number;
  agent_name: string;
  agent_version: string;
  caller: string;
  task_id: string;
  skill_name: string;
  input_hash: string;
  input_preview: string;
  grant_ids: string[];
  file_ops: ReceiptFileOps;
  tool_calls: ReceiptToolCall[];
  artifacts: ReceiptArtifact[];
  handoffs: ReceiptHandoff[];
  status: string;
  error_type: string;
  result_preview: string;
  eval_score: number | null;
  reviewer: string;
  started_at: number;
  ended_at: number;
  elapsed_ms: number;
  nonce: string;
};

export type ReceiptTokenParse =
  | {
      ok: true;
      receipt: ExecutionReceiptPayload;
      /** The exact bytes the signature covers. */
      payloadBytes: Uint8Array;
      signature: Uint8Array;
    }
  | { ok: false; error: string };

const BASE64URL_SEGMENT = /^[A-Za-z0-9_-]*={0,2}$/;

function base64urlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Strict unpadded-base64url decode, matching `a2a_pack.receipts._b64decode`:
 * a segment that does not re-encode to itself is rejected before anything else
 * looks at it, so a token cannot smuggle alternative bytes past the check.
 */
function base64urlDecode(segment: string): Uint8Array {
  const clean = segment.trim();
  if (!BASE64URL_SEGMENT.test(clean)) {
    throw new Error("non-canonical base64url encoding");
  }
  const padded = clean + "=".repeat((4 - (clean.length % 4)) % 4);
  const binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  if (base64urlEncode(bytes) !== clean.replace(/=+$/, "")) {
    throw new Error("non-canonical base64url encoding");
  }
  return bytes;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * Collapse control characters in a string that came out of an unverified
 * payload — the same defence `a2a_pack.cli.receipts_cli._clean` applies, for the
 * same reason: a receipt payload is just base64 until its signature checks out,
 * so a forger controls every string in it. Newlines in a `whitespace-pre-wrap`
 * cell would let a payload draw extra lines — including a fake verdict.
 *
 * Applied at coercion time so nothing downstream can forget it. It never
 * touches `payloadBytes`, so the signature still covers the original bytes.
 */
export function cleanReceiptText(value: string): string {
  let cleaned = "";
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    const isControl = code < 0x20 || (code >= 0x7f && code <= 0x9f);
    cleaned += isControl ? " " : character;
  }
  return cleaned;
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? cleanReceiptText(value) : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function strList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string")
    .map(cleanReceiptText);
}

function fileOps(value: unknown): ReceiptFileOps {
  const raw = isRecord(value) ? value : {};
  return {
    reads: num(raw.reads),
    writes: num(raw.writes),
    bytes_read: num(raw.bytes_read),
    bytes_written: num(raw.bytes_written),
    read_paths_preview: strList(raw.read_paths_preview),
    write_paths_preview: strList(raw.write_paths_preview),
  };
}

function toolCalls(value: unknown): ReceiptToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((raw) => ({
    name: str(raw.name),
    args_hash: str(raw.args_hash),
    status: str(raw.status, "ok"),
    elapsed_ms: num(raw.elapsed_ms),
  }));
}

function artifacts(value: unknown): ReceiptArtifact[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((raw) => ({
    path: str(raw.path),
    mime_type: str(raw.mime_type),
    bytes: num(raw.bytes),
  }));
}

function handoffs(value: unknown): ReceiptHandoff[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((raw) => ({
    callee: str(raw.callee),
    skill: str(raw.skill),
    grant_id: str(raw.grant_id),
    elapsed_ms: num(raw.elapsed_ms),
    status: str(raw.status, "ok"),
  }));
}

const REQUIRED_STRING_FIELDS = ["receipt_id", "agent_name", "skill_name", "status"] as const;

function coerceReceipt(data: unknown): ExecutionReceiptPayload {
  if (!isRecord(data)) throw new Error("payload is not a JSON object");
  for (const field of REQUIRED_STRING_FIELDS) {
    if (typeof data[field] !== "string") {
      throw new Error(`missing or non-string field "${field}"`);
    }
  }
  const evalScore = data.eval_score;
  return {
    receipt_id: str(data.receipt_id),
    schema_version: num(data.schema_version, 1),
    agent_name: str(data.agent_name),
    agent_version: str(data.agent_version),
    caller: str(data.caller),
    task_id: str(data.task_id),
    skill_name: str(data.skill_name),
    input_hash: str(data.input_hash),
    input_preview: str(data.input_preview),
    grant_ids: strList(data.grant_ids),
    file_ops: fileOps(data.file_ops),
    tool_calls: toolCalls(data.tool_calls),
    artifacts: artifacts(data.artifacts),
    handoffs: handoffs(data.handoffs),
    status: str(data.status),
    error_type: str(data.error_type),
    result_preview: str(data.result_preview),
    eval_score: typeof evalScore === "number" && Number.isFinite(evalScore) ? evalScore : null,
    reviewer: str(data.reviewer),
    started_at: num(data.started_at),
    ended_at: num(data.ended_at),
    elapsed_ms: num(data.elapsed_ms),
    nonce: str(data.nonce),
  };
}

/**
 * Split + decode a receipt token. Failure messages mirror the SDK's
 * `ReceiptInvalid` vocabulary so the dashboard and `a2a receipt verify` name the
 * same failure the same way.
 *
 * A successful parse says nothing about authenticity — anyone can mint a token.
 * Only ./receiptVerification can turn this into a claim about who signed it.
 */
export function parseReceiptToken(token: string | null | undefined): ReceiptTokenParse {
  const raw = (token || "").trim();
  if (!raw || !raw.includes(".")) return { ok: false, error: "malformed receipt token" };
  const separator = raw.lastIndexOf(".");
  let payloadBytes: Uint8Array;
  let signature: Uint8Array;
  try {
    payloadBytes = base64urlDecode(raw.slice(0, separator));
    signature = base64urlDecode(raw.slice(separator + 1));
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return { ok: false, error: `receipt decode failed: ${detail}` };
  }
  try {
    const decoded = JSON.parse(new TextDecoder().decode(payloadBytes)) as unknown;
    return { ok: true, receipt: coerceReceipt(decoded), payloadBytes, signature };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return { ok: false, error: `receipt payload invalid: ${detail}` };
  }
}

/**
 * Receipt fields a *sealing observer* fills only when it saw them.
 *
 * On hosted a2a cloud the gateway seals from outside the agent process, after
 * the response completes, so it fills identity / what-ran / outcome / timing
 * and leaves every field below at its default. They are schema fields a richer
 * observer can populate — not pending data — so the UI omits them rather than
 * rendering six empty rows that look like something is still loading.
 */
const OBSERVER_OPTIONAL_FIELDS = [
  "file_ops",
  "tool_calls",
  "artifacts",
  "handoffs",
  "eval_score",
  "reviewer",
] as const;

export type ObserverOptionalField = (typeof OBSERVER_OPTIONAL_FIELDS)[number];

function hasFileOps(ops: ReceiptFileOps): boolean {
  return (
    ops.reads > 0 ||
    ops.writes > 0 ||
    ops.bytes_read > 0 ||
    ops.bytes_written > 0 ||
    ops.read_paths_preview.length > 0 ||
    ops.write_paths_preview.length > 0
  );
}

/** Which observer-optional fields this particular receipt actually carries. */
export function populatedObserverFields(
  receipt: ExecutionReceiptPayload,
): ObserverOptionalField[] {
  const populated: ObserverOptionalField[] = [];
  if (hasFileOps(receipt.file_ops)) populated.push("file_ops");
  if (receipt.tool_calls.length > 0) populated.push("tool_calls");
  if (receipt.artifacts.length > 0) populated.push("artifacts");
  if (receipt.handoffs.length > 0) populated.push("handoffs");
  if (receipt.eval_score !== null) populated.push("eval_score");
  if (receipt.reviewer) populated.push("reviewer");
  return populated;
}

/** The complement: fields this receipt's observer left at their defaults. */
export function unobservedReceiptFields(
  receipt: ExecutionReceiptPayload,
): ObserverOptionalField[] {
  const populated = new Set<ObserverOptionalField>(populatedObserverFields(receipt));
  return OBSERVER_OPTIONAL_FIELDS.filter((field) => !populated.has(field));
}

const OBSERVER_FIELD_LABELS: Record<ObserverOptionalField, string> = {
  file_ops: "file ops",
  tool_calls: "tool calls",
  artifacts: "artifacts",
  handoffs: "handoffs",
  eval_score: "eval score",
  reviewer: "reviewer",
};

function observerFieldLabels(fields: readonly ObserverOptionalField[]): string {
  return fields.map((field) => OBSERVER_FIELD_LABELS[field]).join(", ");
}

type ReceiptFieldRow = {
  label: string;
  value: string;
  /** Empty-but-legitimate (no caller, no error) renders dimmed, not missing. */
  empty: boolean;
  /** Full value for a copy affordance when the rendered value is truncated. */
  copy?: string;
  /** Short caveat rendered beside the value — never marketing, only limits. */
  note?: string;
};

type ReceiptFieldGroupId =
  | "identity"
  | "call"
  | "authority"
  | "effects"
  | "outcome"
  | "timing";

export type ReceiptFieldGroup = {
  id: ReceiptFieldGroupId;
  title: string;
  rows: ReceiptFieldRow[];
  /** Names the observer-optional fields dropped from this group, and why. */
  unobserved?: string;
};

function stamp(seconds: number): string {
  if (!seconds) return "-";
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toISOString().replace(".000Z", "Z");
}

/** The preview limit in `a2a_pack.receipts._preview`. */
export const PREVIEW_LIMIT = 240;

/**
 * Did the sealer truncate this preview, or is it the whole value?
 *
 * `_preview` only truncates when the rendered text is longer than the limit,
 * and then emits exactly `limit - 1` characters plus a horizontal ellipsis. So
 * a preview that is both ellipsis-terminated and exactly `limit` code points
 * long is a truncated one; anything else is the input or result verbatim.
 * Python counts code points, hence the spread rather than `.length`.
 */
export function isTruncatedPreview(preview: string): boolean {
  return preview.endsWith("…") && [...preview].length === PREVIEW_LIMIT;
}

/**
 * What the preview actually is, for this receipt.
 *
 * Calling it "the first 240 characters" in every case reads as a privacy
 * guarantee, and for the common short call it is a false one: the preview *is*
 * the complete input or result. The note follows the value instead of promising
 * a redaction the format does not perform.
 */
export function previewNote(kind: "input" | "result", preview: string): string {
  if (!preview) return `No ${kind} preview was sealed into this receipt.`;
  return isTruncatedPreview(preview)
    ? `Truncated to ${PREVIEW_LIMIT} characters — the rest of the ${kind} is not in the receipt.`
    : `The complete ${kind}, verbatim: it was under the ${PREVIEW_LIMIT}-character preview limit.`;
}

/**
 * The warning /concepts/receipts attaches to handing a token to anyone. It
 * ships beside every copy affordance, because copying is the moment the
 * disclosure happens.
 */
export const RECEIPT_TOKEN_DISCLOSURE =
  "A receipt token is not redacted. Anyone who can base64-decode it reads the " +
  "input and result previews and the run's grant ids — no key needed. Check " +
  "what is in yours before you forward it.";

/** The same warning for a sealed artifact whose schema this view does not decode. */
export const SEALED_TOKEN_DISCLOSURE =
  "This token is not redacted: anyone who can base64-decode it reads whatever " +
  "it embeds, with or without the signing key.";

function fileOpsSummary(ops: ReceiptFileOps): string {
  return (
    `${ops.reads} read / ${ops.writes} written ` +
    `(${ops.bytes_read} B in, ${ops.bytes_written} B out)`
  );
}

function unobservedNote(fields: readonly ObserverOptionalField[]): string | undefined {
  if (fields.length === 0) return undefined;
  return (
    `Not filled by the observer that sealed this receipt: ${observerFieldLabels(fields)}. ` +
    "Hosted runs are sealed by the gateway from outside the agent process, so these " +
    "arrive at their schema defaults — they are not pending data."
  );
}

/**
 * Project a receipt onto the same six groups `a2a receipt show` and
 * /concepts/receipts use: identity, what ran, authority, effects, outcome,
 * timing. Observer-optional fields that this receipt does not carry are omitted
 * and named in the group's `unobserved` note instead of rendering as blanks.
 */
export function groupReceiptFields(receipt: ExecutionReceiptPayload): ReceiptFieldGroup[] {
  const unobserved = new Set(unobservedReceiptFields(receipt));
  const missing = (field: ObserverOptionalField) => unobserved.has(field);

  const effectsRows: ReceiptFieldRow[] = [];
  if (!missing("file_ops")) {
    effectsRows.push({
      label: "files",
      value: fileOpsSummary(receipt.file_ops),
      empty: false,
    });
  }
  if (!missing("tool_calls")) {
    effectsRows.push({
      label: "tools",
      value: receipt.tool_calls
        .map((call) => `${call.name} · ${call.status} · ${call.elapsed_ms}ms`)
        .join("\n"),
      empty: false,
      note: "Arguments are recorded by hash, never by payload.",
    });
  }
  if (!missing("artifacts")) {
    effectsRows.push({
      label: "artifacts",
      value: receipt.artifacts
        .map((artifact) => `${artifact.path} · ${artifact.bytes} B`)
        .join("\n"),
      empty: false,
      note: "Pointers, not bytes.",
    });
  }
  if (!missing("handoffs")) {
    effectsRows.push({
      label: "handoffs",
      value: receipt.handoffs
        .map((handoff) => `${handoff.callee}.${handoff.skill} · ${handoff.status}`)
        .join("\n"),
      empty: false,
    });
  }

  const outcomeRows: ReceiptFieldRow[] = [
    { label: "status", value: receipt.status, empty: false },
    { label: "error", value: receipt.error_type || "-", empty: !receipt.error_type },
    {
      label: "result",
      value: receipt.result_preview || "-",
      empty: !receipt.result_preview,
      copy: receipt.result_preview || undefined,
      note: previewNote("result", receipt.result_preview),
    },
  ];
  if (!missing("eval_score")) {
    outcomeRows.push({
      label: "eval score",
      value: String(receipt.eval_score),
      empty: false,
    });
  }
  if (!missing("reviewer")) {
    outcomeRows.push({ label: "reviewer", value: receipt.reviewer, empty: false });
  }

  const effectsUnobserved = OBSERVER_OPTIONAL_FIELDS.filter(
    (field) => missing(field) && field !== "eval_score" && field !== "reviewer",
  );
  const outcomeUnobserved = OBSERVER_OPTIONAL_FIELDS.filter(
    (field) => missing(field) && (field === "eval_score" || field === "reviewer"),
  );

  return [
    {
      id: "identity",
      title: "identity",
      rows: [
        {
          label: "receipt",
          value: receipt.receipt_id,
          empty: false,
          copy: receipt.receipt_id,
        },
        {
          label: "agent",
          value: receipt.agent_version
            ? `${receipt.agent_name} ${receipt.agent_version}`
            : receipt.agent_name,
          empty: false,
        },
        { label: "caller", value: receipt.caller || "-", empty: !receipt.caller },
        { label: "task", value: receipt.task_id || "-", empty: !receipt.task_id },
      ],
    },
    {
      id: "call",
      title: "what ran",
      rows: [
        { label: "skill", value: receipt.skill_name, empty: false },
        {
          label: "input hash",
          value: receipt.input_hash || "-",
          empty: !receipt.input_hash,
          copy: receipt.input_hash || undefined,
          note: "SHA-256 of the canonicalized input — proves a match, does not reveal the input.",
        },
        {
          label: "input",
          value: receipt.input_preview || "-",
          empty: !receipt.input_preview,
          copy: receipt.input_preview || undefined,
          note: previewNote("input", receipt.input_preview),
        },
      ],
    },
    {
      id: "authority",
      title: "authority",
      rows: [
        {
          label: "grants",
          value: receipt.grant_ids.length > 0 ? receipt.grant_ids.join("\n") : "-",
          empty: receipt.grant_ids.length === 0,
          note: "Grant ids only — the grant bodies live in the grant audit trail.",
        },
      ],
    },
    {
      id: "effects",
      title: "effects",
      rows: effectsRows,
      unobserved: unobservedNote(effectsUnobserved),
    },
    { id: "outcome", title: "outcome", rows: outcomeRows, unobserved: unobservedNote(outcomeUnobserved) },
    {
      id: "timing",
      title: "timing",
      rows: [
        { label: "started", value: stamp(receipt.started_at), empty: !receipt.started_at },
        { label: "ended", value: stamp(receipt.ended_at), empty: !receipt.ended_at },
        { label: "elapsed", value: `${receipt.elapsed_ms}ms`, empty: false },
      ],
    },
  ];
}

/**
 * The exact command that checks this token without the dashboard. Shown when
 * the browser cannot do Ed25519 itself — an honest "check it yourself" beats a
 * green tick the code did not earn.
 */
export function receiptVerifyCommand(token: string): string {
  return `a2a receipt verify --token '${token.trim()}'`;
}

type ReceiptVerificationUnavailableReason =
  | "no-webcrypto"
  | "ed25519-unsupported"
  | "no-published-key";

/**
 * The four honest outcomes of asking "is this signature good?" — checked and
 * valid, checked and invalid, in flight, or not checkable here. There is no
 * fifth state that renders as a tick without a check having happened.
 */
export type ReceiptVerification =
  | { state: "unchecked" }
  | { state: "checking" }
  | { state: "valid"; kid: string }
  | { state: "invalid"; detail: string }
  | {
      state: "unavailable";
      reason: ReceiptVerificationUnavailableReason;
      detail: string;
    };

/** One-line verdict, phrased so it can never overclaim. */
export function receiptVerificationLabel(result: ReceiptVerification): string {
  switch (result.state) {
    case "unchecked":
      return "Signature not checked yet — the fields below are unverified claims.";
    case "checking":
      return "Checking signature...";
    case "valid":
      return `Ed25519 signature valid · key ${result.kid} from /v1/public/receipt-keys`;
    case "invalid":
      return `Signature FAIL — ${result.detail}. Do not trust the fields below.`;
    case "unavailable":
      return `Cannot check here — ${result.detail}`;
  }
}

export function receiptVerificationTone(
  result: ReceiptVerification,
): "emerald" | "red" | "amber" | "neutral" {
  if (result.state === "valid") return "emerald";
  if (result.state === "invalid") return "red";
  if (result.state === "unavailable") return "amber";
  return "neutral";
}

/** Short chip text next to the receipt id. */
export function receiptVerificationBadge(result: ReceiptVerification): string {
  switch (result.state) {
    case "unchecked":
      return "unverified";
    case "checking":
      return "checking";
    case "valid":
      return "signature valid";
    case "invalid":
      return "signature failed";
    case "unavailable":
      return "not checkable here";
  }
}
