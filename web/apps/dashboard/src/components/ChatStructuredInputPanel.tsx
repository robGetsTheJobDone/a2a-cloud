import {
  useEffect,
  useId,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type ReactNode,
} from "react";
import { uploadFile, type FileMeta, type JsonSchema } from "../api";
import {
  EmptyState,
  InlineAlert,
  SelectInput,
  SurfacePanel,
  TextArea,
  TextInput,
  ToolbarButton,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

type JsonObject = Record<string, unknown>;
type FieldPath = Array<string | number>;
type FieldErrors = Record<string, string>;
type SchemaType =
  | "array"
  | "boolean"
  | "integer"
  | "null"
  | "number"
  | "object"
  | "string";

type StructuredFileReference = {
  uri: string;
  path: string;
  name: string;
  mime_type: string;
  size_bytes: number;
};

export type StructuredInputValidationResult = {
  valid: boolean;
  errors: FieldErrors;
};

export type ChatStructuredInputPanelProps = {
  schema: JsonSchema;
  uiSchema?: JsonObject;
  requestId: string;
  value?: JsonObject;
  disabled?: boolean;
  submitLabel?: string;
  title?: string;
  description?: string;
  showHeader?: boolean;
  className?: string;
  onChange?: (next: JsonObject) => void;
  onSubmit?: (value: JsonObject) => Promise<void> | void;
};

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function pathKey(path: FieldPath) {
  return path.length ? path.map(String).join(".") : "$";
}

function fieldName(path: FieldPath) {
  return path.length ? path.map(String).join("_") : "payload";
}

function schemaTypes(schema: JsonSchema): SchemaType[] {
  const rawTypes = Array.isArray(schema.type)
    ? schema.type
    : schema.type
      ? [schema.type]
      : [];
  return rawTypes.filter((type): type is SchemaType =>
    [
      "array",
      "boolean",
      "integer",
      "null",
      "number",
      "object",
      "string",
    ].includes(type),
  );
}

function primaryType(schema: JsonSchema): SchemaType | undefined {
  const explicit = schemaTypes(schema).find((type) => type !== "null");
  if (explicit) return explicit;
  if (schema.properties || schema.additionalProperties !== undefined) return "object";
  if (schema.items) return "array";
  if (schema.enum) return "string";
  if (schema.contentMediaType || schema.format === "a2a-file-ref") return "object";
  return undefined;
}

function fieldTitle(name: string, schema: JsonSchema) {
  return String(schema.title || name);
}

function fieldDescription(schema: JsonSchema) {
  return typeof schema.description === "string" ? schema.description : "";
}

function childUiSchema(uiSchema: JsonObject, key: string) {
  const nested = uiSchema[key];
  return isPlainObject(nested) ? nested : {};
}

function itemUiSchema(uiSchema: JsonObject) {
  const nested = uiSchema.items ?? uiSchema["ui:items"];
  return isPlainObject(nested) ? nested : {};
}

function uiOptionString(
  uiSchema: JsonObject,
  key: string,
  alternateKey?: string,
) {
  const value = uiSchema[key] ?? (alternateKey ? uiSchema[alternateKey] : undefined);
  return typeof value === "string" ? value : undefined;
}

function isPlainObject(value: unknown): value is JsonObject {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isEmptyValue(value: unknown) {
  return (
    value === undefined ||
    value === null ||
    value === "" ||
    (Array.isArray(value) && value.length === 0)
  );
}

function numberKeyword(schema: JsonSchema, key: string) {
  const value = schema[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stringKeyword(schema: JsonSchema, key: string) {
  const value = schema[key];
  return typeof value === "string" ? value : undefined;
}

function deepEqual(a: unknown, b: unknown) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function enumLabel(value: unknown) {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value === null) return "null";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function toFileReference(file: File, meta: FileMeta): StructuredFileReference {
  return {
    uri: `workspace://${meta.path}`,
    path: meta.path,
    name: file.name,
    mime_type: meta.content_type || file.type || "application/octet-stream",
    size_bytes: meta.size,
  };
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function isFileReference(value: unknown): value is Partial<StructuredFileReference> {
  return Boolean(
    value &&
      typeof value === "object" &&
      ("uri" in value || "path" in value || "name" in value),
  );
}

function selectedFileReferences(value: unknown) {
  if (Array.isArray(value)) return value.filter(isFileReference);
  return isFileReference(value) ? [value] : [];
}

function isFileSchema(schema: JsonSchema, uiSchema: JsonObject): boolean {
  const type = primaryType(schema);
  const itemSchema = schema.items;
  return Boolean(
    schema.format === "a2a-file-ref" ||
      schema.contentMediaType !== undefined ||
      uiSchema["ui:widget"] === "file" ||
      uiSchema.widget === "file" ||
      (type === "array" &&
        itemSchema &&
        isFileSchema(itemSchema, itemUiSchema(uiSchema))),
  );
}

function initialValueForSchema(schema: JsonSchema): unknown {
  if (schema.default !== undefined) return schema.default;
  if (schema.const !== undefined) return schema.const;
  if (primaryType(schema) === "boolean") return false;
  return undefined;
}

function initialItemValueForSchema(schema: JsonSchema): unknown {
  const initial = initialValueForSchema(schema);
  if (initial !== undefined) return initial;

  switch (primaryType(schema)) {
    case "object":
      return schema.properties ? createStructuredInputValue(schema) : {};
    case "array":
      return [];
    default:
      return undefined;
  }
}

export function createStructuredInputValue(schema: JsonSchema): JsonObject {
  const out: JsonObject = {};
  for (const [name, field] of Object.entries(schema.properties || {})) {
    const value = initialValueForSchema(field);
    if (value !== undefined) out[name] = value;
  }
  return out;
}

function addError(errors: FieldErrors, path: FieldPath, message: string) {
  const key = pathKey(path);
  if (!errors[key]) errors[key] = message;
}

function validateObjectProperties(
  schema: JsonSchema,
  uiSchema: JsonObject,
  value: JsonObject,
  path: FieldPath,
  errors: FieldErrors,
) {
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);

  for (const name of required) {
    if (isEmptyValue(value[name])) addError(errors, [...path, name], "Required.");
  }

  for (const [name, field] of Object.entries(properties)) {
    validateSchema(
      field,
      childUiSchema(uiSchema, name),
      value[name],
      [...path, name],
      errors,
      required.has(name),
    );
  }

  if (schema.additionalProperties === false) {
    for (const name of Object.keys(value)) {
      if (!properties[name]) addError(errors, [...path, name], "Unknown field.");
    }
  }
}

function validateFileValue(
  schema: JsonSchema,
  value: unknown,
  path: FieldPath,
  errors: FieldErrors,
  required: boolean,
) {
  const isArray = primaryType(schema) === "array";
  const refs = selectedFileReferences(value);
  if (required && refs.length === 0) {
    addError(errors, path, isArray ? "Upload at least one file." : "Upload a file.");
    return;
  }
  if (isEmptyValue(value)) return;

  if (isArray) {
    if (!Array.isArray(value)) {
      addError(errors, path, "Expected a list of files.");
      return;
    }
  } else if (!isFileReference(value)) {
    addError(errors, path, "Expected an uploaded file.");
    return;
  }

  const minItems = numberKeyword(schema, "minItems");
  const maxItems = numberKeyword(schema, "maxItems");
  if (isArray && minItems !== undefined && refs.length < minItems) {
    addError(errors, path, `Upload at least ${minItems} files.`);
  }
  if (isArray && maxItems !== undefined && refs.length > maxItems) {
    addError(errors, path, `Upload no more than ${maxItems} files.`);
  }
}

function validateSchema(
  schema: JsonSchema,
  uiSchema: JsonObject,
  value: unknown,
  path: FieldPath,
  errors: FieldErrors,
  required: boolean,
) {
  if (isFileSchema(schema, uiSchema)) {
    validateFileValue(schema, value, path, errors, required);
    return;
  }

  if (required && isEmptyValue(value)) {
    addError(errors, path, "Required.");
    return;
  }
  if (isEmptyValue(value)) return;

  if (schema.enum && !schema.enum.some((option) => deepEqual(option, value))) {
    addError(errors, path, "Choose one of the allowed values.");
    return;
  }
  if (schema.const !== undefined && !deepEqual(schema.const, value)) {
    addError(errors, path, `Expected ${enumLabel(schema.const)}.`);
    return;
  }

  switch (primaryType(schema)) {
    case "boolean":
      if (typeof value !== "boolean") addError(errors, path, "Expected true or false.");
      return;
    case "integer":
    case "number": {
      if (typeof value !== "number" || !Number.isFinite(value)) {
        addError(errors, path, "Expected a number.");
        return;
      }
      if (primaryType(schema) === "integer" && !Number.isInteger(value)) {
        addError(errors, path, "Expected a whole number.");
        return;
      }
      const minimum = numberKeyword(schema, "minimum");
      const maximum = numberKeyword(schema, "maximum");
      if (minimum !== undefined && value < minimum) {
        addError(errors, path, `Must be at least ${minimum}.`);
      }
      if (maximum !== undefined && value > maximum) {
        addError(errors, path, `Must be no more than ${maximum}.`);
      }
      return;
    }
    case "array": {
      if (!Array.isArray(value)) {
        addError(errors, path, "Expected a list.");
        return;
      }
      const minItems = numberKeyword(schema, "minItems");
      const maxItems = numberKeyword(schema, "maxItems");
      if (minItems !== undefined && value.length < minItems) {
        addError(errors, path, `Add at least ${minItems} items.`);
      }
      if (maxItems !== undefined && value.length > maxItems) {
        addError(errors, path, `Add no more than ${maxItems} items.`);
      }
      if (schema.items) {
        value.forEach((item, index) =>
          validateSchema(
            schema.items as JsonSchema,
            itemUiSchema(uiSchema),
            item,
            [...path, index],
            errors,
            false,
          ),
        );
      }
      return;
    }
    case "object": {
      if (!isPlainObject(value)) {
        addError(errors, path, "Expected a JSON object.");
        return;
      }
      validateObjectProperties(schema, uiSchema, value, path, errors);
      return;
    }
    case "string": {
      if (typeof value !== "string") {
        addError(errors, path, "Expected text.");
        return;
      }
      const minLength = numberKeyword(schema, "minLength");
      const maxLength = numberKeyword(schema, "maxLength");
      const pattern = stringKeyword(schema, "pattern");
      if (minLength !== undefined && value.length < minLength) {
        addError(errors, path, `Use at least ${minLength} characters.`);
      }
      if (maxLength !== undefined && value.length > maxLength) {
        addError(errors, path, `Use no more than ${maxLength} characters.`);
      }
      if (pattern) {
        try {
          if (!new RegExp(pattern).test(value)) {
            addError(errors, path, "Text does not match the required pattern.");
          }
        } catch {
          addError(errors, path, "Schema pattern is invalid.");
        }
      }
      return;
    }
    default:
      return;
  }
}

function validateStructuredInputValue(
  schema: JsonSchema,
  value: JsonObject,
  uiSchema: JsonObject = {},
): StructuredInputValidationResult {
  const errors: FieldErrors = {};
  if (!isPlainObject(value)) {
    addError(errors, [], "Payload must be a JSON object.");
  } else if (schema.properties) {
    validateObjectProperties(schema, uiSchema, value, [], errors);
  } else {
    validateSchema(schema, uiSchema, value, [], errors, true);
  }
  return { valid: Object.keys(errors).length === 0, errors };
}

function describedBy(...ids: Array<string | false | null | undefined>) {
  const joined = ids.filter(Boolean).join(" ");
  return joined || undefined;
}

function fieldError(errors: FieldErrors, path: FieldPath) {
  return errors[pathKey(path)];
}

function errorIdFor(inputId: string) {
  return `${inputId}-error`;
}

export function ChatStructuredInputPanel({
  schema,
  uiSchema = {},
  requestId,
  value,
  disabled = false,
  submitLabel = "Submit",
  title,
  description,
  showHeader = true,
  className,
  onChange,
  onSubmit,
}: ChatStructuredInputPanelProps) {
  const [internalValue, setInternalValue] = useState<JsonObject>(() =>
    createStructuredInputValue(schema),
  );
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submittedOnce, setSubmittedOnce] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const formId = useId();
  const activeValue = value ?? internalValue;
  const propertyEntries = Object.entries(schema.properties || {});
  const required = new Set(schema.required || []);
  const isBusy = disabled || submitting;
  const visibleTitle = title || schema.title || "Structured input";
  const visibleDescription = description || schema.description || "";
  const rootError = propertyEntries.length ? errors[pathKey([])] : undefined;

  useEffect(() => {
    if (value === undefined) setInternalValue(createStructuredInputValue(schema));
    setErrors({});
    setSubmittedOnce(false);
    setSubmitError(null);
  }, [requestId, schema, value]);

  function updateValue(next: JsonObject) {
    if (value === undefined) setInternalValue(next);
    onChange?.(next);
    if (submittedOnce) {
      setErrors(validateStructuredInputValue(schema, next, uiSchema).errors);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isBusy) return;
    const result = validateStructuredInputValue(schema, activeValue, uiSchema);
    setSubmittedOnce(true);
    setErrors(result.errors);
    setSubmitError(null);
    if (!result.valid || !onSubmit) return;
    setSubmitting(true);
    try {
      await onSubmit(activeValue);
    } catch (caught) {
      setSubmitError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      aria-busy={isBusy}
      aria-label={showHeader ? undefined : String(visibleTitle)}
      aria-labelledby={showHeader ? `${formId}-title` : undefined}
      className={cx(
        "rounded-lg border border-runtime-line-soft/60 bg-runtime-bg/80 p-4",
        className,
      )}
      noValidate
      onSubmit={submit}
    >
      {showHeader && (
        <div className="mb-4 border-b border-runtime-line-soft/60 pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 id={`${formId}-title`} className="text-sm font-semibold text-ink">
              {visibleTitle}
            </h3>
            <StatusBadge tone="neutral" className="font-mono">
              {propertyEntries.length
                ? `${propertyEntries.length} field${
                    propertyEntries.length === 1 ? "" : "s"
                  }`
                : "json"}
            </StatusBadge>
          </div>
          {visibleDescription && (
            <p className="mt-1 text-sm text-ink-dim">{visibleDescription}</p>
          )}
        </div>
      )}

      {propertyEntries.length ? (
        <div className="grid gap-3">
          {propertyEntries.map(([name, field]) => (
            <SchemaField
              key={name}
              name={name}
              path={[name]}
              schema={field}
              uiSchema={childUiSchema(uiSchema, name)}
              requestId={requestId}
              value={activeValue[name]}
              required={required.has(name)}
              disabled={isBusy}
              errors={errors}
              onChange={(next) => updateValue({ ...activeValue, [name]: next })}
            />
          ))}
        </div>
      ) : (
        <JsonTextAreaField
          name="payload"
          label="Payload"
          path={[]}
          schema={schema}
          uiSchema={uiSchema}
          requestId={requestId}
          value={activeValue}
          required
          disabled={isBusy}
          errors={errors}
          expected="object"
          onChange={(next) => updateValue(next as JsonObject)}
        />
      )}

      {(rootError || submitError) && (
        <InlineAlert tone="red" role="alert" className="mt-3">
          {rootError || submitError}
        </InlineAlert>
      )}

      {onSubmit && (
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-runtime-line-soft/60 pt-3">
          <ToolbarButton
            type="submit"
            disabled={isBusy}
            variant="primary"
          >
            {submitting ? "Submitting..." : submitLabel}
          </ToolbarButton>
          <span aria-live="polite" className="text-xs text-ink-muted">
            {submittedOnce && Object.keys(errors).length > 0
              ? "Fix the highlighted fields before submitting."
              : "The tool resumes after submission."}
          </span>
        </div>
      )}
    </form>
  );
}

type SchemaFieldProps = {
  name: string;
  path: FieldPath;
  schema: JsonSchema;
  uiSchema: JsonObject;
  requestId: string;
  value: unknown;
  required: boolean;
  disabled: boolean;
  errors: FieldErrors;
  onChange: (next: unknown) => void;
};

function SchemaField(props: SchemaFieldProps) {
  const { schema, uiSchema } = props;
  if (isFileSchema(schema, uiSchema)) return <FileField {...props} />;
  if (schema.enum) return <EnumField {...props} />;

  switch (primaryType(schema)) {
    case "boolean":
      return <BooleanField {...props} />;
    case "integer":
    case "number":
      return <NumberField {...props} />;
    case "array":
      return <ArrayField {...props} />;
    case "object":
      if (schema.properties) return <ObjectField {...props} />;
      return (
        <JsonTextAreaField
          {...props}
          label={fieldTitle(props.name, schema)}
          expected="object"
        />
      );
    default:
      return <StringField {...props} />;
  }
}

function FieldFrame({
  children,
  description,
  error,
  inputId,
  label,
  meta,
  required,
}: {
  children: ReactNode;
  description: string;
  error?: string;
  inputId: string;
  label: string;
  meta?: string;
  required: boolean;
}) {
  const descriptionId = description ? `${inputId}-description` : undefined;
  return (
    <SurfacePanel as="div" className="bg-runtime-panel/45 p-3">
      <div className="flex items-baseline justify-between gap-3">
        <label
          htmlFor={inputId}
          className="text-xs font-medium uppercase text-ink-soft"
        >
          {label}
          {required && <span className="text-signal-authority"> *</span>}
        </label>
        {meta && (
          <StatusBadge tone="neutral" className="font-mono uppercase">
            {meta}
          </StatusBadge>
        )}
      </div>
      {description && (
        <p id={descriptionId} className="mt-1 text-xs text-ink-muted">
          {description}
        </p>
      )}
      {children}
      {error && (
        <InlineAlert
          id={errorIdFor(inputId)}
          tone="red"
          role="alert"
          className="mt-2 text-xs"
        >
          {error}
        </InlineAlert>
      )}
    </SurfacePanel>
  );
}

function StringField({
  name,
  path,
  schema,
  uiSchema,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const inputId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);
  const widget = uiOptionString(uiSchema, "ui:widget", "widget");
  const placeholder = uiOptionString(uiSchema, "ui:placeholder", "placeholder");
  const inputType =
    schema.format === "email"
      ? "email"
      : schema.format === "uri"
        ? "url"
        : schema.format === "date"
          ? "date"
          : "text";

  return (
    <FieldFrame
      description={description}
      error={error}
      inputId={inputId}
      label={fieldTitle(name, schema)}
      meta={schema.format || "text"}
      required={required}
    >
      {widget === "textarea" ? (
        <TextArea
          id={inputId}
          disabled={disabled}
          rows={4}
          value={value == null ? "" : String(value)}
          aria-describedby={describedBy(
            description ? `${inputId}-description` : undefined,
            error ? errorIdFor(inputId) : undefined,
          )}
          aria-invalid={error ? true : undefined}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          invalid={Boolean(error)}
          className="mt-2"
        />
      ) : (
        <TextInput
          id={inputId}
          type={inputType}
          disabled={disabled}
          value={value == null ? "" : String(value)}
          minLength={numberKeyword(schema, "minLength")}
          maxLength={numberKeyword(schema, "maxLength")}
          aria-describedby={describedBy(
            description ? `${inputId}-description` : undefined,
            error ? errorIdFor(inputId) : undefined,
          )}
          aria-invalid={error ? true : undefined}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          invalid={Boolean(error)}
          className="mt-2"
        />
      )}
    </FieldFrame>
  );
}

function NumberField({
  name,
  path,
  schema,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const inputId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);
  const type = primaryType(schema);

  return (
    <FieldFrame
      description={description}
      error={error}
      inputId={inputId}
      label={fieldTitle(name, schema)}
      meta={type || "number"}
      required={required}
    >
      <TextInput
        id={inputId}
        type="number"
        disabled={disabled}
        value={typeof value === "number" && Number.isFinite(value) ? String(value) : ""}
        min={numberKeyword(schema, "minimum")}
        max={numberKeyword(schema, "maximum")}
        step={type === "integer" ? 1 : "any"}
        aria-describedby={describedBy(
          description ? `${inputId}-description` : undefined,
          error ? errorIdFor(inputId) : undefined,
        )}
        aria-invalid={error ? true : undefined}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === "" ? undefined : Number(next));
        }}
        invalid={Boolean(error)}
        className="mt-2"
      />
    </FieldFrame>
  );
}

function BooleanField({
  name,
  path,
  schema,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const inputId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);

  return (
    <SurfacePanel as="div" className="bg-runtime-panel/45 p-3">
      <div className="flex items-start gap-3">
        <input
          id={inputId}
          type="checkbox"
          disabled={disabled}
          checked={Boolean(value)}
          aria-describedby={describedBy(
            description ? `${inputId}-description` : undefined,
            error ? errorIdFor(inputId) : undefined,
          )}
          aria-invalid={error ? true : undefined}
          onChange={(event) => onChange(event.target.checked)}
          className="mt-0.5 h-4 w-4 rounded-md border-runtime-line bg-runtime-bg"
        />
        <div className="min-w-0 flex-1">
          <label
            htmlFor={inputId}
            className="text-xs font-medium uppercase text-ink-soft"
          >
            {fieldTitle(name, schema)}
            {required && <span className="text-signal-authority"> *</span>}
          </label>
          {description && (
            <p id={`${inputId}-description`} className="mt-1 text-xs text-ink-muted">
              {description}
            </p>
          )}
          {error && (
            <InlineAlert
              id={errorIdFor(inputId)}
              tone="red"
              role="alert"
              className="mt-2 text-xs"
            >
              {error}
            </InlineAlert>
          )}
        </div>
      </div>
    </SurfacePanel>
  );
}

function EnumField({
  name,
  path,
  schema,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const inputId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);
  const selectedIndex = schema.enum?.findIndex((option) => deepEqual(option, value)) ?? -1;

  return (
    <FieldFrame
      description={description}
      error={error}
      inputId={inputId}
      label={fieldTitle(name, schema)}
      meta="choice"
      required={required}
    >
      <SelectInput
        id={inputId}
        disabled={disabled}
        value={selectedIndex >= 0 ? String(selectedIndex) : ""}
        aria-describedby={describedBy(
          description ? `${inputId}-description` : undefined,
          error ? errorIdFor(inputId) : undefined,
        )}
        aria-invalid={error ? true : undefined}
        onChange={(event) => {
          const selected = event.target.value;
          onChange(selected === "" ? undefined : schema.enum?.[Number(selected)]);
        }}
        invalid={Boolean(error)}
        className="mt-2"
      >
        <option value="">Select...</option>
        {(schema.enum || []).map((option, index) => (
          <option key={`${index}-${enumLabel(option)}`} value={String(index)}>
            {enumLabel(option)}
          </option>
        ))}
      </SelectInput>
    </FieldFrame>
  );
}

function FileField({
  name,
  path,
  schema,
  uiSchema,
  requestId,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const inputId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const validationError = fieldError(errors, path);
  const description = fieldDescription(schema);
  const refs = selectedFileReferences(value);
  const itemSchema = schema.items;
  const multiple = primaryType(schema) === "array";
  const accept =
    uiOptionString(uiSchema, "accept", "ui:accept") ||
    schema.contentMediaType ||
    itemSchema?.contentMediaType ||
    "";
  const error = uploadError || validationError;

  function resetInput() {
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function addFiles(files: FileList | File[]) {
    const picked = Array.from(files).filter((file) => file.name);
    if (!picked.length || disabled || uploading) return;
    setUploading(true);
    setUploadError(null);
    try {
      const nextRefs: StructuredFileReference[] = [];
      for (const file of picked) {
        const meta = await uploadFile(file, `inputs/${requestId}`);
        nextRefs.push(toFileReference(file, meta));
      }
      onChange(multiple ? [...refs, ...nextRefs] : nextRefs[0]);
    } catch (caught) {
      setUploadError(errorMessage(caught));
    } finally {
      setUploading(false);
      resetInput();
    }
  }

  function onDrag(event: DragEvent<HTMLElement>, isOver: boolean) {
    event.preventDefault();
    if (disabled || uploading) return;
    setDragging(isOver);
  }

  return (
    <SurfacePanel
      as="div"
      className={cx(
        "bg-runtime-panel/45 p-3 transition-colors",
        dragging && "border-signal-authority ring-2 ring-signal-authority/20",
      )}
      onDragEnter={(event) => onDrag(event, true)}
      onDragLeave={(event) => onDrag(event, false)}
      onDragOver={(event) => onDrag(event, true)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        void addFiles(event.dataTransfer.files);
      }}
    >
      <div className="flex items-baseline justify-between gap-3">
        <label
          htmlFor={inputId}
          className="text-xs font-medium uppercase text-ink-soft"
        >
          {fieldTitle(name, schema)}
          {required && <span className="text-signal-authority"> *</span>}
        </label>
        <StatusBadge tone="neutral" className="font-mono uppercase">
          {multiple ? "files" : "file"}
        </StatusBadge>
      </div>
      {description && (
        <p id={`${inputId}-description`} className="mt-1 text-xs text-ink-muted">
          {description}
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          ref={fileInputRef}
          id={inputId}
          type="file"
          multiple={multiple}
          accept={accept}
          disabled={disabled || uploading}
          aria-describedby={describedBy(
            description ? `${inputId}-description` : undefined,
            error ? errorIdFor(inputId) : undefined,
          )}
          aria-invalid={error ? true : undefined}
          onChange={(event) => void addFiles(event.target.files || [])}
          className="sr-only"
        />
        <ToolbarButton
          type="button"
          disabled={disabled || uploading}
          onClick={() => fileInputRef.current?.click()}
          size="sm"
        >
          {uploading ? "Uploading..." : multiple ? "Choose files" : "Choose file"}
        </ToolbarButton>
        <span className="text-xs text-ink-muted">
          {dragging ? "Drop to upload" : accept || "Any file type"}
        </span>
      </div>

      {refs.length > 0 && (
        <ul aria-label={`${fieldTitle(name, schema)} uploads`} className="mt-3 space-y-2">
          {refs.map((ref, index) => {
            const displayName = ref.name || ref.path || ref.uri || `File ${index + 1}`;
            return (
              <SurfacePanel
                as="li"
                key={`${ref.path || ref.uri || displayName}-${index}`}
                className="flex min-w-0 items-center gap-2 bg-runtime-bg px-2 py-1.5 text-xs"
              >
                <span className="min-w-0 flex-1 truncate font-mono text-signal-live">
                  {displayName}
                </span>
                {typeof ref.size_bytes === "number" && (
                  <span className="shrink-0 text-ink-faint">
                    {formatBytes(ref.size_bytes)}
                  </span>
                )}
                <ToolbarButton
                  type="button"
                  disabled={disabled || uploading}
                  aria-label={`Remove ${displayName}`}
                  onClick={() => {
                    const next = refs.filter((_, refIndex) => refIndex !== index);
                    onChange(multiple ? next : undefined);
                  }}
                  variant="ghost"
                  size="xs"
                  className="shrink-0 hover:text-signal-danger"
                >
                  Remove
                </ToolbarButton>
              </SurfacePanel>
            );
          })}
        </ul>
      )}

      {error && (
        <InlineAlert
          id={errorIdFor(inputId)}
          tone="red"
          role="alert"
          className="mt-2 text-xs"
        >
          {error}
        </InlineAlert>
      )}
    </SurfacePanel>
  );
}

function ArrayField({
  name,
  path,
  schema,
  uiSchema,
  requestId,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const legendId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);
  const descriptionId = description ? `${legendId}-description` : undefined;
  const itemSchema = schema.items || { type: "string" };
  const items = Array.isArray(value) ? value : [];

  function updateItem(index: number, next: unknown) {
    onChange(items.map((item, itemIndex) => (itemIndex === index ? next : item)));
  }

  function removeItem(index: number) {
    onChange(items.filter((_, itemIndex) => itemIndex !== index));
  }

  return (
    <SurfacePanel
      as="fieldset"
      aria-describedby={describedBy(
        descriptionId,
        error ? `${legendId}-error` : undefined,
      )}
      className="bg-runtime-panel/45 p-3"
    >
      <legend id={legendId} className="w-full">
        <span className="flex items-baseline justify-between gap-3">
          <span className="text-xs font-medium uppercase text-ink-soft">
            {fieldTitle(name, schema)}
            {required && <span className="text-signal-authority"> *</span>}
          </span>
          <StatusBadge tone="neutral" className="font-mono">
            {items.length} item{items.length === 1 ? "" : "s"}
          </StatusBadge>
        </span>
      </legend>
      {description && (
        <p id={descriptionId} className="mt-1 text-xs text-ink-muted">
          {description}
        </p>
      )}

      <div className="mt-3 space-y-3">
        {items.length === 0 ? (
          <EmptyState title="No items added." size="compact" className="py-4" />
        ) : (
          items.map((item, index) => (
            <div key={`${fieldName(path)}-${index}`} className="grid gap-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-ink-muted">
                  Item {index + 1}
                </span>
                <ToolbarButton
                  type="button"
                  disabled={disabled}
                  onClick={() => removeItem(index)}
                  variant="ghost"
                  size="xs"
                  className="hover:text-signal-danger"
                >
                  Remove
                </ToolbarButton>
              </div>
              <SchemaField
                name={`Item ${index + 1}`}
                path={[...path, index]}
                schema={itemSchema}
                uiSchema={itemUiSchema(uiSchema)}
                requestId={requestId}
                value={item}
                required={false}
                disabled={disabled}
                errors={errors}
                onChange={(next) => updateItem(index, next)}
              />
            </div>
          ))
        )}
      </div>

      {error && (
        <InlineAlert
          id={`${legendId}-error`}
          tone="red"
          role="alert"
          className="mt-2 text-xs"
        >
          {error}
        </InlineAlert>
      )}

      <ToolbarButton
        type="button"
        disabled={disabled}
        onClick={() => onChange([...items, initialItemValueForSchema(itemSchema)])}
        className="mt-3"
      >
        Add item
      </ToolbarButton>
    </SurfacePanel>
  );
}

function ObjectField({
  name,
  path,
  schema,
  uiSchema,
  requestId,
  value,
  required,
  disabled,
  errors,
  onChange,
}: SchemaFieldProps) {
  const legendId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);
  const descriptionId = description ? `${legendId}-description` : undefined;
  const objectValue = isPlainObject(value) ? value : {};
  const entries = Object.entries(schema.properties || {});
  const requiredFields = new Set(schema.required || []);

  return (
    <SurfacePanel
      as="fieldset"
      aria-describedby={describedBy(
        descriptionId,
        error ? `${legendId}-error` : undefined,
      )}
      className="bg-runtime-panel/45 p-3"
    >
      <legend id={legendId} className="w-full">
        <span className="flex items-baseline justify-between gap-3">
          <span className="text-xs font-medium uppercase text-ink-soft">
            {fieldTitle(name, schema)}
            {required && <span className="text-signal-authority"> *</span>}
          </span>
          <StatusBadge tone="neutral" className="font-mono uppercase">
            object
          </StatusBadge>
        </span>
      </legend>
      {description && (
        <p id={descriptionId} className="mt-1 text-xs text-ink-muted">
          {description}
        </p>
      )}
      {error && (
        <InlineAlert
          id={`${legendId}-error`}
          tone="red"
          role="alert"
          className="mt-2 text-xs"
        >
          {error}
        </InlineAlert>
      )}

      <div className="mt-3 grid gap-3">
        {entries.map(([childName, field]) => (
          <SchemaField
            key={childName}
            name={childName}
            path={[...path, childName]}
            schema={field}
            uiSchema={childUiSchema(uiSchema, childName)}
            requestId={requestId}
            value={objectValue[childName]}
            required={requiredFields.has(childName)}
            disabled={disabled}
            errors={errors}
            onChange={(next) => onChange({ ...objectValue, [childName]: next })}
          />
        ))}
      </div>
    </SurfacePanel>
  );
}

function JsonTextAreaField({
  path,
  schema,
  value,
  required,
  disabled,
  errors,
  onChange,
  label,
  expected,
}: SchemaFieldProps & {
  label: string;
  expected: "array" | "object";
}) {
  const inputId = useId();
  const error = fieldError(errors, path);
  const description = fieldDescription(schema);
  const [text, setText] = useState(() =>
    JSON.stringify(value ?? (expected === "array" ? [] : {}), null, 2),
  );
  const [focused, setFocused] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  const serialized = JSON.stringify(value ?? (expected === "array" ? [] : {}), null, 2);
  const visibleError = parseError || error;

  useEffect(() => {
    if (!focused) setText(serialized);
  }, [focused, serialized]);

  return (
    <FieldFrame
      description={description}
      error={visibleError}
      inputId={inputId}
      label={label}
      meta="json"
      required={required}
    >
      <TextArea
        id={inputId}
        disabled={disabled}
        rows={6}
        value={text}
        aria-describedby={describedBy(
          description ? `${inputId}-description` : undefined,
          visibleError ? errorIdFor(inputId) : undefined,
        )}
        aria-invalid={visibleError ? true : undefined}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            const parsed = JSON.parse(next || (expected === "array" ? "[]" : "{}"));
            if (expected === "array" && !Array.isArray(parsed)) {
              setParseError("Expected a JSON array.");
              return;
            }
            if (expected === "object" && !isPlainObject(parsed)) {
              setParseError("Expected a JSON object.");
              return;
            }
            setParseError(null);
            onChange(parsed);
          } catch (caught) {
            setParseError(errorMessage(caught));
          }
        }}
        invalid={Boolean(visibleError)}
        mono
        compact
        className="mt-2"
      />
    </FieldFrame>
  );
}
