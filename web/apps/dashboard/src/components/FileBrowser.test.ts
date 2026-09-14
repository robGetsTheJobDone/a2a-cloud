import { describe, expect, it } from "vitest";
import type { FileMeta } from "../api";
import { diffFileEntries } from "./FileBrowser";

function file(path: string, overrides: Partial<FileMeta> = {}): FileMeta {
  return {
    path,
    size: 10,
    modified_at: "2026-07-18T12:00:00Z",
    content_type: "text/plain",
    etag: `${path}:v1`,
    ...overrides,
  };
}

describe("live file browser change detection", () => {
  it("detects creates, same-size rewrites, and deletes by object revision", () => {
    const before = [
      file("updated.txt"),
      file("deleted.txt"),
    ];
    const after = [
      file("updated.txt", { etag: "updated.txt:v2" }),
      file("created.txt"),
    ];

    expect(diffFileEntries(before, after, 123)).toEqual([
      {
        kind: "update",
        path: "updated.txt",
        detectedAt: 123,
        before: before[0],
        after: after[0],
      },
      {
        kind: "create",
        path: "created.txt",
        detectedAt: 123,
        after: after[1],
      },
      {
        kind: "delete",
        path: "deleted.txt",
        detectedAt: 123,
        before: before[1],
      },
    ]);
  });

  it("falls back to metadata and ignores directory-only changes", () => {
    const directory = file("outputs", { is_dir: true, etag: undefined });
    const unchanged = file("report.md", { etag: undefined });

    expect(
      diffFileEntries(
        [directory, unchanged],
        [
          { ...directory, modified_at: "2026-07-18T12:01:00Z" },
          { ...unchanged },
        ],
      ),
    ).toEqual([]);
  });
});
