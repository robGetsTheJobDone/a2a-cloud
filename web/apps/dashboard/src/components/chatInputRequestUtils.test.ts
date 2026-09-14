import { describe, expect, it } from "vitest";
import type { JsonSchema } from "../api";
import {
  countFileFields,
  formatBytes,
  initialFormValue,
  inputRequestMeta,
  isFileSchema,
  missingRequired,
  selectedFileRefs,
} from "./chatInputRequestUtils";

describe("chat input request helpers", () => {
  it("builds initial values from default, const, and boolean fields", () => {
    const schema: JsonSchema = {
      type: "object",
      properties: {
        name: { type: "string", default: "Ada" },
        mode: { type: "string", const: "review" },
        approved: { type: "boolean" },
        notes: { type: "string" },
      },
    };

    expect(initialFormValue(schema)).toEqual({
      name: "Ada",
      mode: "review",
      approved: false,
    });
  });

  it("flags only empty required values as missing", () => {
    const schema: JsonSchema = {
      type: "object",
      required: ["name", "tags", "enabled", "count"],
    };

    expect(
      missingRequired(schema, {
        name: "",
        tags: [],
        enabled: false,
        count: 0,
      }),
    ).toEqual(["name", "tags"]);
  });

  it("detects file fields from schema formats, media types, arrays, and UI widgets", () => {
    const fileSchema: JsonSchema = { type: "object", format: "a2a-file-ref" };
    const arraySchema: JsonSchema = {
      type: "array",
      items: { type: "object", contentMediaType: "image/png" },
    };

    expect(isFileSchema(fileSchema, {})).toBe(true);
    expect(isFileSchema(arraySchema, {})).toBe(true);
    expect(isFileSchema({ type: "string" }, { "ui:widget": "file" })).toBe(true);
    expect(isFileSchema({ type: "string" }, {})).toBe(false);
  });

  it("counts top-level file inputs for the compact event meta", () => {
    const schema: JsonSchema = {
      type: "object",
      properties: {
        avatar: { type: "object", format: "a2a-file-ref" },
        attachments: {
          type: "array",
          items: { type: "object", contentMediaType: "application/pdf" },
        },
        title: { type: "string" },
      },
    };

    expect(countFileFields(schema, {})).toBe(2);
  });

  it("normalizes selected file references", () => {
    const refs = selectedFileRefs([
      { path: "inputs/a.txt", size_bytes: 10 },
      { label: "not a file ref" },
      { uri: "workspace://inputs/b.txt" },
    ]);

    expect(refs).toEqual([
      { path: "inputs/a.txt", size_bytes: 10 },
      { uri: "workspace://inputs/b.txt" },
    ]);
  });

  it("formats byte counts and condensed meta labels", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(2 * 1024 * 1024)).toBe("2.0 MB");
    expect(inputRequestMeta(3, 2, 1)).toBe("3 fields / 2 required / 1 file field");
    expect(inputRequestMeta(0, 0, 0)).toBeNull();
  });
});
