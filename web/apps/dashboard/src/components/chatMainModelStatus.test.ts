import { describe, expect, it } from "vitest";
import type { LLMCreds } from "../api";
import { resolveChatMainModelStatus } from "./chatMainModelStatus";

function cred(name: string, model: string): LLMCreds {
  return {
    id: name === "default" ? 1 : 2,
    name,
    base_url: "https://api.example.test/v1",
    model,
    temperature_mode: "omit",
    temperature: null,
    extra_body: {},
    api_key_redacted: "sk-...",
    created_at: "2026-06-25T00:00:00.000Z",
    updated_at: "2026-06-25T00:00:00.000Z",
  };
}

describe("resolveChatMainModelStatus", () => {
  it("blocks chat while saved keys are still loading", () => {
    const status = resolveChatMainModelStatus([], "", false);

    expect(status.canChat).toBe(false);
    expect(status.loading).toBe(true);
    expect(status.label).toBe("checking LLM keys");
  });

  it("blocks chat when no saved key is available", () => {
    const status = resolveChatMainModelStatus([], "", true);

    expect(status.canChat).toBe(false);
    expect(status.blocker).toContain("Add an LLM key");
    expect(status.placeholder).toContain("Add an LLM key");
  });

  it("uses the default key without sending an explicit credential name", () => {
    const status = resolveChatMainModelStatus(
      [cred("default", "gpt-4.1")],
      "",
      true,
    );

    expect(status.canChat).toBe(true);
    expect(status.requestCredsName).toBeUndefined();
    expect(status.label).toBe("gpt-4.1 · default key");
  });

  it("uses the selected saved key when it exists", () => {
    const status = resolveChatMainModelStatus(
      [cred("default", "gpt-4.1"), cred("kimi", "kimi-k2")],
      "kimi",
      true,
    );

    expect(status.canChat).toBe(true);
    expect(status.requestCredsName).toBe("kimi");
    expect(status.label).toBe("kimi-k2 · kimi");
  });

  it("recovers from stale selected keys by falling back to default or first key", () => {
    const withDefault = resolveChatMainModelStatus(
      [cred("default", "gpt-4.1"), cred("kimi", "kimi-k2")],
      "deleted-key",
      true,
    );
    const withoutDefault = resolveChatMainModelStatus(
      [cred("kimi", "kimi-k2")],
      "deleted-key",
      true,
    );

    expect(withDefault.requestCredsName).toBeUndefined();
    expect(withDefault.label).toBe("gpt-4.1 · default key");
    expect(withoutDefault.requestCredsName).toBe("kimi");
    expect(withoutDefault.label).toBe("kimi-k2 · kimi");
  });
});
