/**
 * Verification is checked against a real Ed25519 keypair, not a stub: the test
 * signs a payload with WebCrypto, hands the raw public half to the module the
 * way /v1/public/receipt-keys would, and asserts PASS. The tamper case flips a
 * byte of the signed payload and asserts FAIL — the same two cases the
 * orchestrator ran through `a2a receipt verify`.
 */
import { describe, expect, it } from "vitest";
import type { ReceiptKeys } from "../api";
import { verifyReceiptToken } from "./receiptVerification";

const subtle = globalThis.crypto.subtle;

function utf8(value: string) {
  return new TextEncoder().encode(value);
}

function b64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function b64url(bytes: Uint8Array): string {
  return b64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const PAYLOAD = JSON.stringify({
  receipt_id: "4b4d56883bb4d526",
  schema_version: 1,
  agent_name: "research-agent",
  agent_version: "",
  caller: "user:42",
  task_id: "",
  skill_name: "ask",
  input_hash: "ad6b174eb01357d875c98f53aef35ca9c96437ed54de7974b507a5221563a435",
  input_preview: '{"prompt": "say hello"}',
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
});

async function sealedReceipt(): Promise<{ token: string; keys: ReceiptKeys }> {
  const pair = (await subtle.generateKey({ name: "Ed25519" }, true, [
    "sign",
    "verify",
  ])) as CryptoKeyPair;
  const rawPublic = new Uint8Array(await subtle.exportKey("raw", pair.publicKey));
  const payload = utf8(PAYLOAD);
  const signature = new Uint8Array(
    await subtle.sign({ name: "Ed25519" }, pair.privateKey, payload),
  );
  return {
    token: `${b64url(payload)}.${b64url(signature)}`,
    keys: {
      active_kid: "kid-under-test",
      keys: [
        { kid: "unrelated", alg: "Ed25519", public_key: b64(new Uint8Array(32)), use: "receipt" },
        { kid: "kid-under-test", alg: "Ed25519", public_key: b64(rawPublic), use: "receipt" },
      ],
    },
  };
}

function keysFrom(keys: ReceiptKeys) {
  return () => Promise.resolve(keys);
}

describe("verifyReceiptToken", () => {
  it("passes a genuine receipt against the published active key", async () => {
    const { token, keys } = await sealedReceipt();
    await expect(verifyReceiptToken(token, { fetchKeys: keysFrom(keys), subtle })).resolves.toEqual(
      { state: "valid", kid: "kid-under-test" },
    );
  });

  it("fails a one-field-tampered payload carrying the original signature", async () => {
    const { token, keys } = await sealedReceipt();
    const [, signature] = token.split(".");
    const tampered = PAYLOAD.replace('"status":"ok"', '"status":"error"');
    expect(tampered).not.toBe(PAYLOAD);

    const result = await verifyReceiptToken(`${b64url(utf8(tampered))}.${signature}`, {
      fetchKeys: keysFrom(keys),
      subtle,
    });

    expect(result.state).toBe("invalid");
    if (result.state !== "invalid") throw new Error("expected an invalid result");
    expect(result.detail).toContain("signature mismatch");
  });

  it("fails a receipt checked against a different key", async () => {
    const { token } = await sealedReceipt();
    const other = await sealedReceipt();
    const result = await verifyReceiptToken(token, {
      fetchKeys: keysFrom(other.keys),
      subtle,
    });
    expect(result.state).toBe("invalid");
  });

  it("reports a malformed token without ever fetching a key", async () => {
    let fetched = false;
    const result = await verifyReceiptToken("not-a-token", {
      fetchKeys: () => {
        fetched = true;
        return Promise.reject(new Error("should not be called"));
      },
      subtle,
    });
    expect(result).toEqual({ state: "invalid", detail: "malformed receipt token" });
    expect(fetched).toBe(false);
  });

  it("says it cannot check when the browser has no WebCrypto", async () => {
    const { token, keys } = await sealedReceipt();
    const result = await verifyReceiptToken(token, {
      fetchKeys: keysFrom(keys),
      subtle: null,
    });
    expect(result).toMatchObject({ state: "unavailable", reason: "no-webcrypto" });
  });

  it("says it cannot check when the browser has no Ed25519", async () => {
    const { token, keys } = await sealedReceipt();
    const withoutEd25519 = {
      importKey: () => Promise.reject(new Error("Unrecognized name.")),
      verify: () => Promise.reject(new Error("unreachable")),
    } as unknown as SubtleCrypto;

    const result = await verifyReceiptToken(token, {
      fetchKeys: keysFrom(keys),
      subtle: withoutEd25519,
    });

    expect(result).toMatchObject({ state: "unavailable", reason: "ed25519-unsupported" });
    if (result.state !== "unavailable") throw new Error("expected an unavailable result");
    expect(result.detail).toContain("Unrecognized name.");
  });

  it("says it cannot check when the platform publishes no key", async () => {
    const { token } = await sealedReceipt();

    await expect(
      verifyReceiptToken(token, {
        fetchKeys: () => Promise.reject(new Error("503 receipt verifying key is not configured")),
        subtle,
      }),
    ).resolves.toMatchObject({ state: "unavailable", reason: "no-published-key" });

    await expect(
      verifyReceiptToken(token, {
        fetchKeys: keysFrom({ active_kid: "", keys: [] }),
        subtle,
      }),
    ).resolves.toMatchObject({ state: "unavailable", reason: "no-published-key" });
  });
});
