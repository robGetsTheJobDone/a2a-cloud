import { afterEach, describe, expect, it, vi } from "vitest";

import {
  captureStudioPrefillFromHash,
  clearStoredStudioPrefill,
  readStoredStudioPrefill,
} from "./studioPrefill";

function browserWithHash(hash: string) {
  const values = new Map<string, string>();
  const replaceState = vi.fn();
  vi.stubGlobal("window", {
    location: { pathname: "/studio", search: "?source=idea-funnel", hash },
    history: { replaceState },
    sessionStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  });
  return { replaceState, values };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Agent Studio funnel prefill", () => {
  it("captures a valid private fragment for the post-auth Studio screen", () => {
    const goal = "Triage support messages and draft grounded replies.";
    const { replaceState } = browserWithHash(
      `#goal=${encodeURIComponent(goal)}&name=support-triage-a1b2c3`,
    );

    expect(captureStudioPrefillFromHash()).toEqual({
      goal,
      name: "support-triage-a1b2c3",
    });
    expect(readStoredStudioPrefill()).toEqual({
      goal,
      name: "support-triage-a1b2c3",
    });
    expect(replaceState).toHaveBeenCalledWith(
      null,
      "",
      "/studio?source=idea-funnel",
    );

    clearStoredStudioPrefill();
    expect(readStoredStudioPrefill()).toBeNull();
  });

  it("rejects malformed names instead of preloading them into a build", () => {
    const { replaceState } = browserWithHash(
      "#goal=Build%20a%20useful%20support%20agent&name=unsafe%20name%3Bdeploy",
    );

    expect(captureStudioPrefillFromHash()).toBeNull();
    expect(readStoredStudioPrefill()).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
