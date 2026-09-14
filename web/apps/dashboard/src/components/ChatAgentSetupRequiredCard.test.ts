import { describe, expect, it } from "vitest";
import type { ConsumerSetupField, ConsumerSetupStatus } from "../api";
import {
  initialSetupValues,
  payloadSetupValues,
  setupHtmlInputType,
} from "./ChatAgentSetupRequiredCard";

const fields: ConsumerSetupField[] = [
  {
    name: "api_key",
    kind: "secret",
    label: "API key",
    description: "",
    required: true,
    input_type: "password",
    options: [],
  },
  {
    name: "enabled",
    kind: "config",
    label: "Enabled",
    description: "",
    required: false,
    input_type: "boolean",
    options: [],
  },
  {
    name: "limit",
    kind: "config",
    label: "Limit",
    description: "",
    required: false,
    input_type: "number",
    options: [],
  },
];

function setupStatus(): ConsumerSetupStatus {
  return {
    declaration: { fields },
    values: [
      {
        name: "api_key",
        kind: "secret",
        configured: true,
        source: "user",
        value_redacted: "sk_...",
        updated_at: null,
      },
    ],
    missing_required: [],
    complete: false,
    organization: null,
    can_manage_org: false,
  };
}

describe("chat agent setup helpers", () => {
  it("starts inputs only for fields without configured values", () => {
    expect(initialSetupValues(setupStatus())).toEqual({
      enabled: "",
      limit: "",
    });
  });

  it("builds payloads without dropping false or zero values", () => {
    expect(
      payloadSetupValues(fields, {
        api_key: "",
        enabled: false,
        limit: 0,
      }),
    ).toEqual({
      enabled: false,
      limit: 0,
    });
  });

  it("keeps only browser-supported setup input types", () => {
    expect(setupHtmlInputType("url")).toBe("url");
    expect(setupHtmlInputType("email")).toBe("email");
    expect(setupHtmlInputType("number")).toBe("number");
    expect(setupHtmlInputType("password")).toBe("text");
    expect(setupHtmlInputType("unknown")).toBe("text");
  });
});
