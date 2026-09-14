import type { JsonSchema } from "../api";

export type SelectedFileRef = {
  uri?: string;
  path?: string;
  size_bytes?: number;
};

export function countFileFields(
  schema: JsonSchema,
  uiSchema: Record<string, unknown>,
): number {
  return Object.entries(schema.properties || {}).filter(([name, field]) =>
    isFileSchema(
      field,
      (uiSchema[name] as Record<string, unknown> | undefined) || {},
    ),
  ).length;
}

export function isFileSchema(
  schema: JsonSchema,
  uiSchema: Record<string, unknown>,
): boolean {
  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  const itemSchema =
    schema.items && !Array.isArray(schema.items) ? schema.items : undefined;
  return Boolean(
    schema.format === "a2a-file-ref" ||
      schema.contentMediaType !== undefined ||
      uiSchema["ui:widget"] === "file" ||
      (type === "array" && itemSchema && isFileSchema(itemSchema, uiSchema)),
  );
}

export function initialFormValue(schema: JsonSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, field] of Object.entries(schema.properties || {})) {
    if (field.default !== undefined) out[name] = field.default;
    else if (field.const !== undefined) out[name] = field.const;
    else if (field.type === "boolean") out[name] = false;
  }
  return out;
}

export function missingRequired(
  schema: JsonSchema,
  value: Record<string, unknown>,
): string[] {
  return (schema.required || []).filter((name) => {
    const v = value[name];
    return v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length);
  });
}

function isFileRef(value: unknown): value is SelectedFileRef {
  return Boolean(
    value && typeof value === "object" && ("uri" in value || "path" in value),
  );
}

export function selectedFileRefs(value: unknown): SelectedFileRef[] {
  if (Array.isArray(value)) return value.filter(isFileRef);
  return isFileRef(value) ? [value] : [];
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function inputRequestMeta(
  propertyCount: number,
  requiredCount: number,
  fileFieldCount: number,
) {
  const parts = [
    propertyCount > 0 ? `${propertyCount} ${plural("field", propertyCount)}` : null,
    requiredCount > 0 ? `${requiredCount} required` : null,
    fileFieldCount > 0
      ? `${fileFieldCount} file ${plural("field", fileFieldCount)}`
      : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" / ") : null;
}

function plural(noun: string, count: number) {
  return count === 1 ? noun : `${noun}s`;
}
