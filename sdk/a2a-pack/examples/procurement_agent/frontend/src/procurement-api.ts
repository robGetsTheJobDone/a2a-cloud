import {
  invokeCompareQuotes,
  invokeCreatePurchaseRequest,
  invokeDecidePurchaseRequest,
  invokeExecutiveDashboard,
  invokeIngestQuotePayloads,
  invokeSupplierScorecard,
} from "./agent-client";
import { createClient } from "./agent-client/client";

type PackedFrontendConfig = {
  endpoints: {
    invoke: string;
  };
};

type SkillEnvelope = {
  result: unknown;
  events: unknown[];
  artifacts: unknown[];
  grant_id: string | null;
};

export type QuotePayload = {
  filename: string;
  data_base64: string;
  media_type?: string;
};

function apiBaseFromConfig(config: PackedFrontendConfig): string {
  const invoke = config.endpoints.invoke.replace(/\/+$/, "");
  return invoke.endsWith("/invoke") ? invoke.slice(0, -"/invoke".length) || "/" : invoke;
}

function unwrap(payload: unknown): unknown {
  return (payload as SkillEnvelope).result;
}

export function createProcurementApi(config: PackedFrontendConfig) {
  const client = createClient({
    baseUrl: apiBaseFromConfig(config),
    credentials: "same-origin",
    responseStyle: "data",
    throwOnError: true,
  });

  return {
    async ingestQuotePayloads(args: {
      documents: QuotePayload[];
      requested_part?: string;
      requested_quantity?: number | null;
    }) {
      const payload = await invokeIngestQuotePayloads({
        client,
        body: { arguments: args },
        responseStyle: "data",
        throwOnError: true,
      });
      return unwrap(payload);
    },

    async compareQuotes(args: { part_query: string; quantity?: number | null }) {
      const payload = await invokeCompareQuotes({
        client,
        body: { arguments: args },
        responseStyle: "data",
        throwOnError: true,
      });
      return unwrap(payload);
    },

    async createPurchaseRequest(args: {
      quote_line_item_id: number;
      quantity: number;
      reason?: string;
    }) {
      const payload = await invokeCreatePurchaseRequest({
        client,
        body: { arguments: args },
        responseStyle: "data",
        throwOnError: true,
      });
      return unwrap(payload);
    },

    async decidePurchaseRequest(args: {
      request_id: number;
      decision: "approve" | "reject" | "request_more_quotes";
      note?: string;
    }) {
      const payload = await invokeDecidePurchaseRequest({
        client,
        body: { arguments: args },
        responseStyle: "data",
        throwOnError: true,
      });
      return unwrap(payload);
    },

    async supplierScorecard(args: { supplier_name: string }) {
      const payload = await invokeSupplierScorecard({
        client,
        body: { arguments: args },
        responseStyle: "data",
        throwOnError: true,
      });
      return unwrap(payload);
    },

    async executiveDashboard() {
      const payload = await invokeExecutiveDashboard({
        client,
        body: { arguments: {} },
        responseStyle: "data",
        throwOnError: true,
      });
      return unwrap(payload);
    },
  };
}

