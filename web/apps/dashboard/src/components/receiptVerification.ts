/**
 * Client-side Ed25519 verification of a receipt token.
 *
 * This module is loaded on demand (see ReceiptEvidence) so neither WebCrypto
 * glue nor the key fetch is part of first paint.
 *
 * The check is the real one: the published verifying key from
 * `GET /v1/public/receipt-keys` is imported as a raw Ed25519 public key and
 * `crypto.subtle.verify` is run over the exact payload bytes the signature
 * covers. There is no decorative path — when the browser cannot do Ed25519, or
 * the platform publishes no key, the result says so and the UI falls back to
 * printing the `a2a receipt verify` command.
 */
import { getReceiptKeys, type ReceiptKeys } from "../api";
import { parseReceiptToken, type ReceiptVerification } from "./receiptToken";

export type ReceiptVerificationDeps = {
  fetchKeys?: () => Promise<ReceiptKeys>;
  subtle?: SubtleCrypto | null;
};

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value.trim());
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/**
 * Hand WebCrypto a plain ArrayBuffer. A `Uint8Array` may be backed by a
 * SharedArrayBuffer, which `BufferSource` excludes, so copying here keeps the
 * call sites free of casts.
 */
function bufferSource(bytes: Uint8Array): ArrayBuffer {
  const buffer = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(buffer).set(bytes);
  return buffer;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function defaultSubtle(): SubtleCrypto | null {
  const webcrypto = (globalThis as { crypto?: Crypto }).crypto;
  return webcrypto?.subtle ?? null;
}

/**
 * Verify `token` against the platform's published key.
 *
 * Never throws: every failure resolves to a state the UI can state plainly.
 */
export async function verifyReceiptToken(
  token: string,
  deps: ReceiptVerificationDeps = {},
): Promise<ReceiptVerification> {
  const parsed = parseReceiptToken(token);
  if (!parsed.ok) return { state: "invalid", detail: parsed.error };

  const subtle = deps.subtle === undefined ? defaultSubtle() : deps.subtle;
  if (!subtle) {
    return {
      state: "unavailable",
      reason: "no-webcrypto",
      detail: "WebCrypto is unavailable in this context (it needs a secure origin).",
    };
  }

  let published: ReceiptKeys;
  try {
    published = await (deps.fetchKeys ?? getReceiptKeys)();
  } catch (error) {
    return {
      state: "unavailable",
      reason: "no-published-key",
      detail: `Could not read the published verifying key: ${errorText(error)}`,
    };
  }

  const keys = published.keys || [];
  const active =
    keys.find((candidate) => candidate.kid === published.active_kid) ?? keys[0] ?? null;
  if (!active?.public_key) {
    return {
      state: "unavailable",
      reason: "no-published-key",
      detail: "The control plane publishes no receipt verifying key.",
    };
  }

  // Decoded before the import so unreadable key material is reported as a key
  // problem rather than blamed on the browser.
  let keyBytes: ArrayBuffer;
  try {
    keyBytes = bufferSource(decodeBase64(active.public_key));
  } catch (error) {
    return {
      state: "unavailable",
      reason: "no-published-key",
      detail: `The published verifying key is unreadable: ${errorText(error)}`,
    };
  }

  let key: CryptoKey;
  try {
    key = await subtle.importKey("raw", keyBytes, { name: "Ed25519" }, false, ["verify"]);
  } catch (error) {
    return {
      state: "unavailable",
      reason: "ed25519-unsupported",
      detail: `This browser cannot check Ed25519 signatures: ${errorText(error)}`,
    };
  }

  let ok: boolean;
  try {
    ok = await subtle.verify(
      { name: "Ed25519" },
      key,
      bufferSource(parsed.signature),
      bufferSource(parsed.payloadBytes),
    );
  } catch (error) {
    return {
      state: "unavailable",
      reason: "ed25519-unsupported",
      detail: `This browser cannot check Ed25519 signatures: ${errorText(error)}`,
    };
  }

  return ok
    ? { state: "valid", kid: active.kid }
    : {
        state: "invalid",
        detail: "receipt signature mismatch — not signed by the published key",
      };
}
