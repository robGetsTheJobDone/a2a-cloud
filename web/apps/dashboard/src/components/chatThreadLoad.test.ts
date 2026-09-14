import { describe, expect, it } from "vitest";
import { shouldDeferThreadContentLoad } from "./chatThreadLoad";

describe("shouldDeferThreadContentLoad", () => {
  it("defers persisted reloads for the thread created by the active stream", () => {
    expect(
      shouldDeferThreadContentLoad({
        threadId: "thread-1",
        streaming: true,
        localStreamThreadId: "thread-1",
      }),
    ).toBe(true);
  });

  it("does not defer initial route loads without a local streaming owner", () => {
    expect(
      shouldDeferThreadContentLoad({
        threadId: "thread-1",
        streaming: true,
        localStreamThreadId: null,
      }),
    ).toBe(false);
  });

  it("does not defer loads for a different selected thread", () => {
    expect(
      shouldDeferThreadContentLoad({
        threadId: "thread-2",
        streaming: true,
        localStreamThreadId: "thread-1",
      }),
    ).toBe(false);
  });

  it("does not defer after streaming completes", () => {
    expect(
      shouldDeferThreadContentLoad({
        threadId: "thread-1",
        streaming: false,
        localStreamThreadId: "thread-1",
      }),
    ).toBe(false);
  });
});
