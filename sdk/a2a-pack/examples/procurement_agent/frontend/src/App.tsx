import {
  BarChart3,
  Check,
  FileText,
  RefreshCw,
  Search,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createProcurementApi, type QuotePayload } from "./procurement-api";

type ProcurementApi = ReturnType<typeof createProcurementApi>;

type PackedFrontendConfig = {
  endpoints: {
    invoke: string;
    session: string;
  };
};

type SessionPayload = {
  authenticated?: boolean;
  user?: { email?: string | null } | null;
  org?: { slug?: string | null } | null;
};

type QuoteLine = {
  id: number;
  supplier_name: string;
  unit_price: string;
  unit_price_cents: number;
  quoted_total?: string;
  delivery_days?: number | null;
  part_name: string;
  unit?: string;
};

type Comparison = {
  quotes?: QuoteLine[];
  historical_range?: { min: string; max: string } | null;
  recommendation?: {
    quote_line_item_id: number;
    supplier: string;
    unit_price: string;
    delivery_days?: number | null;
    reason: string;
  } | null;
};

type PurchaseRequest = {
  id: number;
  status: string;
  part_name: string;
  quantity: string;
  unit?: string;
  recommended_supplier_name: string;
  amount: string;
};

type Dashboard = {
  purchases_this_month: string;
  suppliers_reviewed: number;
  late_deliveries: number;
  pending_approvals: number;
};

type Scorecard = {
  found: boolean;
  supplier?: string;
  quotes_submitted?: number;
  average_delivery_days?: number | null;
  price_score?: number;
  delivery_score?: number;
  quality_score?: number;
  overall_score?: number;
};

const sampleQuotes = [
  {
    filename: "beta-industrial.txt",
    media_type: "text/plain",
    text: `Supplier: Beta Industrial
Part: 6205 bearing
SKU: BRG-6205
Quantity: 500
Unit: unit
Unit Price: BRL 9.10
Delivery: 8 days
Payment Terms: Net 15
Valid Until: 2026-07-01`,
  },
  {
    filename: "acme-bearings.txt",
    media_type: "text/plain",
    text: `Supplier: Acme Bearings
Part: 6205 bearing
SKU: BRG-6205
Quantity: 500
Unit: unit
Unit Price: BRL 10.75
Delivery: 12 days
Payment Terms: Net 30
Valid Until: 2026-07-01`,
  },
  {
    filename: "nova-supply.txt",
    media_type: "text/plain",
    text: `Supplier: Nova Supply
Part: 6205 bearing
SKU: BRG-6205
Quantity: 500
Unit: unit
Unit Price: BRL 9.60
Delivery: 6 days
Payment Terms: Net 30
Valid Until: 2026-07-01`,
  },
];

function textPayload(item: (typeof sampleQuotes)[number]): QuotePayload {
  return {
    filename: item.filename,
    media_type: item.media_type,
    data_base64: btoa(unescape(encodeURIComponent(item.text))),
  };
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

async function fileToPayload(file: File): Promise<QuotePayload> {
  return {
    filename: file.name,
    media_type: file.type || "application/octet-stream",
    data_base64: bytesToBase64(new Uint8Array(await file.arrayBuffer())),
  };
}

function money(cents: number): string {
  return `BRL ${(cents / 100).toFixed(2)}`;
}

function createDemoApi(): ProcurementApi {
  const quotes: QuoteLine[] = [];
  let request: PurchaseRequest | null = null;
  let totalCents = 0;

  function read(text: string, label: string): string {
    return text.match(new RegExp(`^${label}:\\s*(.+)$`, "im"))?.[1]?.trim() || "";
  }

  function parseQuote(payload: QuotePayload, id: number): QuoteLine {
    const text = decodeURIComponent(escape(atob(payload.data_base64)));
    const price = Number((read(text, "Unit Price").match(/[0-9]+(?:[.,][0-9]+)?/) || ["0"])[0].replace(",", "."));
    const delivery = Number((read(text, "Delivery").match(/[0-9]+/) || ["14"])[0]);
    const quantity = Number(read(text, "Quantity") || 1);
    return {
      id,
      supplier_name: read(text, "Supplier") || payload.filename.replace(/\.[^.]+$/, ""),
      part_name: read(text, "Part") || "Unknown part",
      unit_price: `BRL ${price.toFixed(2)}`,
      unit_price_cents: Math.round(price * 100),
      quoted_total: money(Math.round(price * 100) * quantity),
      delivery_days: delivery,
      unit: read(text, "Unit") || "unit",
    };
  }

  return {
    async ingestQuotePayloads(args) {
      const parsed = args.documents.map((doc, index) => parseQuote(doc, quotes.length + index + 1));
      quotes.push(...parsed);
      return {
        ingested_count: parsed.length,
        error_count: 0,
        quotes: parsed.map((line) => ({ line_item: line })),
        errors: [],
      };
    },
    async compareQuotes(args) {
      const sorted = [...quotes].sort(
        (a, b) => a.unit_price_cents - b.unit_price_cents || (a.delivery_days || 999) - (b.delivery_days || 999),
      );
      const best = sorted[0];
      return {
        quote_count: sorted.length,
        historical_range: sorted.length
          ? {
              min: money(Math.min(...sorted.map((quote) => quote.unit_price_cents))),
              max: money(Math.max(...sorted.map((quote) => quote.unit_price_cents))),
            }
          : null,
        quotes: sorted.map((quote) => ({
          ...quote,
          quoted_total: money(quote.unit_price_cents * Number(args.quantity || 1)),
        })),
        recommendation: best
          ? {
              quote_line_item_id: best.id,
              supplier: best.supplier_name,
              unit_price: best.unit_price,
              delivery_days: best.delivery_days,
              reason: "Lowest effective cost after delivery penalty.",
            }
          : null,
      };
    },
    async createPurchaseRequest(args) {
      const quote = quotes.find((item) => item.id === args.quote_line_item_id);
      request = {
        id: 1,
        status: "pending_approval",
        part_name: quote?.part_name || "Unknown part",
        quantity: String(args.quantity),
        unit: quote?.unit || "unit",
        recommended_supplier_name: quote?.supplier_name || "Unknown supplier",
        amount: money((quote?.unit_price_cents || 0) * args.quantity),
      };
      return { request, approval_actions: ["approve", "reject", "request_more_quotes"] };
    },
    async decidePurchaseRequest(args) {
      const state = {
        approve: "approved",
        reject: "rejected",
        request_more_quotes: "more_quotes_requested",
      }[args.decision];
      request = request ? { ...request, status: state } : null;
      if (args.decision === "approve" && request) {
        totalCents += Math.round(Number(request.amount.replace(/[^0-9.]/g, "")) * 100);
      }
      return { request, approval: { decision: args.decision, note: args.note } };
    },
    async supplierScorecard(args) {
      return {
        supplier: args.supplier_name,
        found: true,
        quotes_submitted: quotes.filter((quote) => quote.supplier_name === args.supplier_name).length,
        average_delivery_days: 8,
        price_score: 92,
        delivery_score: 96,
        quality_score: 91,
        overall_score: 93.2,
      };
    },
    async executiveDashboard() {
      return {
        purchases_this_month: money(totalCents),
        suppliers_reviewed: new Set(quotes.map((quote) => quote.supplier_name)).size,
        late_deliveries: 0,
        pending_approvals: request?.status === "pending_approval" ? 1 : 0,
      };
    },
  } as ProcurementApi;
}

function result<T>(value: unknown): T {
  return value as T;
}

export function App() {
  const [api, setApi] = useState<ProcurementApi>(() => createDemoApi());
  const [sessionLabel, setSessionLabel] = useState("Connecting");
  const [status, setStatus] = useState("Ready");
  const [files, setFiles] = useState<File[]>([]);
  const [partQuery, setPartQuery] = useState("6205 bearing");
  const [quantity, setQuantity] = useState(500);
  const [comparison, setComparison] = useState<Comparison>({});
  const [selectedQuoteId, setSelectedQuoteId] = useState<number | null>(null);
  const [request, setRequest] = useState<PurchaseRequest | null>(null);
  const [supplierName, setSupplierName] = useState("Beta Industrial");
  const [scorecard, setScorecard] = useState<Scorecard | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard>({
    purchases_this_month: "BRL 0.00",
    suppliers_reviewed: 0,
    late_deliveries: 0,
    pending_approvals: 0,
  });

  useEffect(() => {
    async function load() {
      try {
        const config = (await fetch("./config.json", { credentials: "same-origin" }).then((response) => {
          if (!response.ok) throw new Error("config unavailable");
          return response.json();
        })) as PackedFrontendConfig;
        const nextApi = createProcurementApi(config);
        setApi(() => nextApi);
        const session = (await fetch(config.endpoints.session, { credentials: "same-origin" })
          .then((response) => (response.ok ? response.json() : null))
          .catch(() => null)) as SessionPayload | null;
        const email = session?.user?.email || "A2A session";
        const org = session?.org?.slug ? ` / ${session.org.slug}` : "";
        setSessionLabel(`${email}${org}`);
        const data = result<Dashboard>(await nextApi.executiveDashboard());
        setDashboard(data);
      } catch {
        const demoApi = createDemoApi();
        setApi(() => demoApi);
        setSessionLabel("Demo mode");
        setDashboard(result<Dashboard>(await demoApi.executiveDashboard()));
      }
    }
    void load();
  }, []);

  const quotes = comparison.quotes || [];
  const selectedQuote = useMemo(
    () => quotes.find((quote) => quote.id === selectedQuoteId) || null,
    [quotes, selectedQuoteId],
  );

  async function refreshDashboard() {
    setDashboard(result<Dashboard>(await api.executiveDashboard()));
  }

  async function ingestSelected() {
    setStatus("Reading quote files");
    const documents = files.length
      ? await Promise.all(files.map(fileToPayload))
      : sampleQuotes.map(textPayload);
    const ingested = result<{ ingested_count: number; error_count: number }>(
      await api.ingestQuotePayloads({
        documents,
        requested_part: partQuery,
        requested_quantity: quantity,
      }),
    );
    setStatus(`Ingested ${ingested.ingested_count} quotes`);
    await compareQuotes();
  }

  async function compareQuotes() {
    setStatus("Comparing quotes");
    const data = result<Comparison>(
      await api.compareQuotes({
        part_query: partQuery,
        quantity,
      }),
    );
    setComparison(data);
    setSelectedQuoteId(data.recommendation?.quote_line_item_id || null);
    if (data.recommendation?.supplier) setSupplierName(data.recommendation.supplier);
    setStatus(data.recommendation ? "Recommendation ready" : "No quote history found");
  }

  async function createRequest() {
    if (!selectedQuoteId) return;
    const data = result<{ request: PurchaseRequest }>(
      await api.createPurchaseRequest({
        quote_line_item_id: selectedQuoteId,
        quantity,
        reason: `Recommended quote for ${partQuery}`,
      }),
    );
    setRequest(data.request);
    setStatus("Purchase request created");
    await refreshDashboard();
  }

  async function decide(decision: "approve" | "reject" | "request_more_quotes") {
    if (!request) return;
    try {
      const data = result<{ request: PurchaseRequest }>(
        await api.decidePurchaseRequest({
          request_id: request.id,
          decision,
          note: "Best overall quote.",
        }),
      );
      setRequest(data.request);
      setStatus(`Request ${data.request.status}`);
      await refreshDashboard();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Decision failed");
    }
  }

  async function refreshScorecard() {
    if (!supplierName.trim()) return;
    setScorecard(result<Scorecard>(await api.supplierScorecard({ supplier_name: supplierName.trim() })));
  }

  return (
    <main className="min-h-screen px-4 py-4 text-zinc-900 sm:px-6">
      <header className="flex flex-col gap-3 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <p className="text-xs font-bold uppercase text-emerald-700">Procurement system</p>
          <h1 className="text-3xl font-semibold tracking-normal text-zinc-950">Quote intelligence</h1>
        </div>
        <div className="flex items-center gap-2">
          <div className="min-h-9 rounded-full border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-600">
            {sessionLabel}
          </div>
          <button
            className="grid h-9 w-9 place-items-center rounded-md border border-zinc-200 bg-white text-zinc-700 hover:bg-zinc-50"
            type="button"
            onClick={() => void refreshDashboard()}
            title="Refresh dashboard"
            aria-label="Refresh dashboard"
          >
            <RefreshCw size={17} />
          </button>
        </div>
      </header>

      <section className="grid gap-3 pb-4 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi label="Purchases This Month" value={dashboard.purchases_this_month} />
        <Kpi label="Suppliers Reviewed" value={String(dashboard.suppliers_reviewed)} />
        <Kpi label="Late Deliveries" value={String(dashboard.late_deliveries)} />
        <Kpi label="Pending Approvals" value={String(dashboard.pending_approvals)} />
      </section>

      <section className="grid items-start gap-4 xl:grid-cols-[minmax(280px,0.9fr)_minmax(430px,1.45fr)_minmax(320px,1fr)]">
        <Panel>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase text-zinc-500">Quote intake</p>
              <h2 className="text-lg font-semibold">Supplier quotes</h2>
            </div>
            <button
              type="button"
              onClick={() => {
                setFiles([]);
                setStatus("Sample quotes ready");
              }}
              className="rounded-md px-2 py-1 text-sm font-semibold text-blue-700 hover:bg-blue-50"
            >
              Sample
            </button>
          </div>

          <Field label="Part or SKU">
            <input
              className="h-10 w-full rounded-md border border-zinc-300 bg-white px-3 outline-none focus:border-emerald-600"
              value={partQuery}
              onChange={(event) => setPartQuery(event.target.value)}
            />
          </Field>
          <Field label="Quantity">
            <input
              className="h-10 w-full rounded-md border border-zinc-300 bg-white px-3 outline-none focus:border-emerald-600"
              type="number"
              min={1}
              value={quantity}
              onChange={(event) => setQuantity(Number(event.target.value || 1))}
            />
          </Field>

          <label className="mb-3 grid min-h-36 cursor-pointer place-items-center rounded-lg border border-dashed border-emerald-300 bg-emerald-50 px-4 py-6 text-center text-emerald-950 hover:bg-emerald-100">
            <input
              className="hidden"
              type="file"
              multiple
              accept="application/pdf,text/plain,.txt,.pdf"
              onChange={(event) => setFiles(Array.from(event.target.files || []))}
            />
            <UploadCloud size={24} />
            <span className="mt-2 text-sm font-bold">Quote files</span>
            <span className="mt-1 text-xs text-emerald-800">
              {files.length ? `${files.length} selected` : "No files selected"}
            </span>
          </label>

          <div className="flex gap-2">
            <button
              className="inline-flex h-10 flex-1 items-center justify-center gap-2 rounded-md bg-emerald-700 px-3 text-sm font-bold text-white hover:bg-emerald-800"
              type="button"
              onClick={() => void ingestSelected()}
            >
              <FileText size={16} />
              Ingest
            </button>
            <button
              className="inline-flex h-10 flex-1 items-center justify-center gap-2 rounded-md border border-blue-200 bg-blue-50 px-3 text-sm font-bold text-blue-800 hover:bg-blue-100"
              type="button"
              onClick={() => void compareQuotes()}
            >
              <Search size={16} />
              Compare
            </button>
          </div>
          <p className="mt-3 min-h-5 text-sm text-zinc-600">{status}</p>
        </Panel>

        <Panel className="xl:row-span-2">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase text-zinc-500">Price intelligence</p>
              <h2 className="text-lg font-semibold">Recommendation</h2>
            </div>
            <div className="rounded-full border border-zinc-200 bg-zinc-50 px-3 py-1 text-xs font-semibold text-zinc-600">
              {comparison.historical_range
                ? `${comparison.historical_range.min} - ${comparison.historical_range.max}`
                : "No history"}
            </div>
          </div>

          <div className="mb-4 min-h-28 rounded-lg border border-zinc-200 bg-zinc-50 p-4">
            <p className="text-sm text-zinc-600">
              {comparison.recommendation?.reason || "No recommendation yet"}
            </p>
            <strong className="mt-2 block text-xl font-semibold text-zinc-950">
              {comparison.recommendation
                ? `${comparison.recommendation.supplier} at ${comparison.recommendation.unit_price}`
                : "Upload or compare quotes"}
            </strong>
            {comparison.recommendation ? (
              <span className="mt-2 block text-sm text-zinc-600">
                {comparison.recommendation.delivery_days || "-"} day delivery
              </span>
            ) : null}
          </div>

          <div className="overflow-hidden rounded-lg border border-zinc-200">
            <div className="hidden min-h-9 grid-cols-[1.4fr_0.8fr_0.7fr_0.9fr] gap-3 bg-zinc-100 px-3 py-2 text-xs font-bold text-zinc-600 md:grid">
              <span>Supplier</span>
              <span>Unit Price</span>
              <span>Delivery</span>
              <span>Total</span>
            </div>
            {quotes.length ? (
              quotes.map((quote) => (
                <button
                  key={quote.id}
                  type="button"
                  onClick={() => setSelectedQuoteId(quote.id)}
                  className={`grid min-h-14 w-full gap-2 border-t border-zinc-200 px-3 py-3 text-left text-sm md:grid-cols-[1.4fr_0.8fr_0.7fr_0.9fr] ${
                    quote.id === selectedQuoteId ? "bg-emerald-50" : "bg-white hover:bg-zinc-50"
                  }`}
                >
                  <strong>{quote.supplier_name}</strong>
                  <span>{quote.unit_price}</span>
                  <span>{quote.delivery_days || "-"} days</span>
                  <span>{quote.quoted_total || "-"}</span>
                </button>
              ))
            ) : (
              <div className="px-3 py-6 text-sm text-zinc-500">No quotes loaded.</div>
            )}
          </div>

          <button
            className="mt-4 inline-flex h-10 items-center justify-center gap-2 rounded-md bg-zinc-950 px-4 text-sm font-bold text-white disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!selectedQuote}
            onClick={() => void createRequest()}
          >
            <ShieldCheck size={16} />
            Create Purchase Request
          </button>
        </Panel>

        <Panel>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase text-zinc-500">Approval queue</p>
              <h2 className="text-lg font-semibold">Purchase request</h2>
            </div>
            <span className="rounded-full border border-zinc-200 bg-zinc-50 px-3 py-1 text-xs font-semibold text-zinc-600">
              {request?.status || "None"}
            </span>
          </div>
          <div className="mb-3 min-h-28 rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-sm leading-6 text-zinc-700">
            {request ? (
              <>
                <strong className="text-zinc-950">{request.part_name}</strong>
                <br />
                {request.quantity} {request.unit || "unit"} from {request.recommended_supplier_name}
                <br />
                Amount: {request.amount}
              </>
            ) : (
              "No request created."
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            <DecisionButton disabled={request?.status !== "pending_approval"} onClick={() => void decide("approve")}>
              <Check size={15} />
              Approve
            </DecisionButton>
            <DecisionButton disabled={request?.status !== "pending_approval"} onClick={() => void decide("reject")}>
              <X size={15} />
              Reject
            </DecisionButton>
            <DecisionButton disabled={request?.status !== "pending_approval"} onClick={() => void decide("request_more_quotes")}>
              More
            </DecisionButton>
          </div>
        </Panel>

        <Panel>
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase text-zinc-500">Supplier memory</p>
              <h2 className="text-lg font-semibold">Scorecard</h2>
            </div>
            <button
              type="button"
              onClick={() => void refreshScorecard()}
              className="grid h-9 w-9 place-items-center rounded-md border border-zinc-200 bg-white text-zinc-700 hover:bg-zinc-50"
              title="Refresh scorecard"
              aria-label="Refresh scorecard"
            >
              <BarChart3 size={17} />
            </button>
          </div>
          <Field label="Supplier">
            <input
              className="h-10 w-full rounded-md border border-zinc-300 bg-white px-3 outline-none focus:border-emerald-600"
              value={supplierName}
              onChange={(event) => setSupplierName(event.target.value)}
            />
          </Field>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-2">
            <Score label="Price" value={scorecard?.price_score} />
            <Score label="Delivery" value={scorecard?.delivery_score} />
            <Score label="Quality" value={scorecard?.quality_score} />
            <Score label="Overall" value={scorecard?.overall_score} />
          </div>
          <p className="mt-3 min-h-10 text-sm leading-6 text-zinc-600">
            {scorecard?.found
              ? `${scorecard.quotes_submitted || 0} quotes submitted. Average delivery: ${
                  scorecard.average_delivery_days || "-"
                } days.`
              : "No supplier selected."}
          </p>
        </Panel>
      </section>
    </main>
  );
}

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-zinc-200 bg-white p-4 shadow-panel ${className}`}>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="mb-3 grid gap-1.5 text-sm font-bold text-zinc-700">
      {label}
      {children}
    </label>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-h-20 rounded-lg border border-zinc-200 bg-white p-4 shadow-panel">
      <span className="block text-xs font-bold text-zinc-500">{label}</span>
      <strong className="mt-2 block text-2xl font-semibold tracking-normal text-zinc-950">{value}</strong>
    </div>
  );
}

function Score({ label, value }: { label: string; value?: number }) {
  return (
    <div className="min-h-16 rounded-lg border border-zinc-200 bg-zinc-50 p-3">
      <span className="block text-xs font-bold text-zinc-500">{label}</span>
      <strong className="mt-1 block text-xl font-semibold text-zinc-950">
        {value === undefined ? "-" : value}
      </strong>
    </div>
  );
}

function DecisionButton({
  children,
  disabled,
  onClick,
}: {
  children: React.ReactNode;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className="inline-flex min-h-10 items-center justify-center gap-1 rounded-md border border-zinc-200 bg-white px-2 text-sm font-bold text-zinc-800 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50"
      type="button"
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

