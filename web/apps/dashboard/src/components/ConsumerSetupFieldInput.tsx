import type { ConsumerSetupField } from "../api";
import { SelectInput, TextArea, TextInput } from "./DashboardChrome";

export function ConsumerSetupFieldInput({
  field,
  value,
  attention,
  disabled,
  onChange,
  className = "mt-3",
}: {
  field: ConsumerSetupField;
  value: unknown;
  attention: boolean;
  disabled: boolean;
  onChange: (value: unknown) => void;
  className?: string;
}) {
  if (field.input_type === "boolean") {
    return (
      <SelectInput
        value={value === true ? "true" : value === false ? "false" : ""}
        onChange={(event) =>
          onChange(event.target.value === "" ? "" : event.target.value === "true")
        }
        disabled={disabled}
        attention={attention}
        className={className}
      >
        <option value="">Select</option>
        <option value="true">True</option>
        <option value="false">False</option>
      </SelectInput>
    );
  }

  if (field.input_type === "select") {
    return (
      <SelectInput
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        attention={attention}
        className={className}
      >
        <option value="">Select</option>
        {field.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </SelectInput>
    );
  }

  if (field.input_type === "textarea") {
    return (
      <TextArea
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        rows={3}
        attention={attention}
        className={className}
      />
    );
  }

  return (
    <TextInput
      type={field.kind === "secret" ? "password" : setupHtmlInputType(field.input_type)}
      value={typeof value === "string" || typeof value === "number" ? value : ""}
      onChange={(event) =>
        onChange(
          field.input_type === "number" && event.target.value !== ""
            ? Number(event.target.value)
            : event.target.value,
        )
      }
      disabled={disabled}
      autoComplete={field.kind === "secret" ? "new-password" : "off"}
      attention={attention}
      className={className}
    />
  );
}

function setupHtmlInputType(inputType: string) {
  if (inputType === "password") return "password";
  if (inputType === "url") return "url";
  if (inputType === "email") return "email";
  if (inputType === "number") return "number";
  return "text";
}
