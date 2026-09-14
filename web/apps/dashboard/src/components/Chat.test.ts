import { describe, expect, it } from "vitest";
import {
  appendProgressEntry,
  chatOrganizationSlugFromList,
  type ProgressEntry,
} from "./Chat";

describe("chat organization routing", () => {
  it("uses the first preferred organization slug when available", () => {
    expect(
      chatOrganizationSlugFromList([
        { slug: "personal-7" },
        { slug: "acme" },
      ]),
    ).toBe("personal-7");
  });

  it("omits organization scope when the preference lookup has no rows", () => {
    expect(chatOrganizationSlugFromList([])).toBeUndefined();
  });
});

describe("appendProgressEntry", () => {
  it("reuses the current progress list for duplicate consecutive events", () => {
    const entries: ProgressEntry[] = [
      { kind: "deploy", message: "Uploading source" },
    ];

    const next = appendProgressEntry(entries, {
      kind: "deploy",
      message: "Uploading source",
    });

    expect(next).toBe(entries);
  });

  it("keeps the latest progress events bounded", () => {
    const entries: ProgressEntry[] = Array.from({ length: 12 }, (_, index) => ({
      kind: "deploy",
      message: `step-${index}`,
    }));

    const next = appendProgressEntry(entries, {
      kind: "deploy",
      message: "step-12",
    });

    expect(next).toHaveLength(12);
    expect(next[0]).toEqual({ kind: "deploy", message: "step-1" });
    expect(next[11]).toEqual({ kind: "deploy", message: "step-12" });
  });
});
