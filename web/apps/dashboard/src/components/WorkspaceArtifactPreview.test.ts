import { describe, expect, it } from "vitest";
import { buildTextDiff } from "./WorkspaceArtifactPreview";

describe("workspace artifact text diffs", () => {
  it("builds a compact line comparison with context", () => {
    const diff = buildTextDiff(
      ["alpha", "beta", "old value", "omega"].join("\n"),
      ["alpha", "beta", "new value", "extra", "omega"].join("\n"),
    );

    expect(diff).not.toBeNull();
    expect(diff?.added).toBe(2);
    expect(diff?.removed).toBe(1);
    expect(diff?.lines.filter((line) => line.kind === "remove").map((line) => line.text))
      .toEqual(["old value"]);
    expect(diff?.lines.filter((line) => line.kind === "add").map((line) => line.text))
      .toEqual(["new value", "extra"]);
    expect(diff?.lines.some((line) => line.kind === "context" && line.text === "omega"))
      .toBe(true);
  });

  it("returns no diff for identical contents and collapses distant context", () => {
    expect(buildTextDiff("same", "same")).toBeNull();

    const before = Array.from({ length: 12 }, (_, index) => `line ${index}`);
    const after = [...before];
    after[6] = "changed";
    const diff = buildTextDiff(before.join("\n"), after.join("\n"));

    expect(diff?.lines.some((line) => line.kind === "skip")).toBe(true);
    expect(diff?.added).toBe(1);
    expect(diff?.removed).toBe(1);
  });
});
