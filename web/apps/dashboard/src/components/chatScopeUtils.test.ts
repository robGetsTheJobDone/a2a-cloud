import { describe, expect, it } from "vitest";
import { writeScopeLabels } from "./chatScopeUtils";

describe("chat scope helpers", () => {
  it("uses explicit write prefixes before legacy fallbacks", () => {
    expect(
      writeScopeLabels({
        write_prefixes: ["workspace/out", "workspace/logs"],
        write_prefix: "legacy/write",
        outputs_prefix: "legacy/output",
      }),
    ).toEqual(["workspace/out", "workspace/logs"]);
  });

  it("falls back to single write and output prefixes", () => {
    expect(writeScopeLabels({ write_prefix: "workspace/write" })).toEqual([
      "workspace/write",
    ]);
    expect(writeScopeLabels({ outputs_prefix: "workspace/output" })).toEqual([
      "workspace/output",
    ]);
  });

  it("drops empty prefixes and returns an empty list when no write scope exists", () => {
    expect(writeScopeLabels({ write_prefixes: ["", "workspace/out"] })).toEqual([
      "workspace/out",
    ]);
    expect(writeScopeLabels({ write_prefixes: [] })).toEqual([]);
  });
});
