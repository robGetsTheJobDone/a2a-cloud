/**
 * ReceiptEvidence — the signed execution receipt, shown as a receipt.
 *
 * Runs carry sealed evidence artifacts (`SubagentRun.receipts`), and the
 * headline one is an Ed25519-signed execution receipt. This renders it in the
 * same six groups `a2a receipt show` and /concepts/receipts use, and lets the
 * owner actually check the signature instead of taking the row on faith.
 *
 * Two rules this component exists to keep:
 *
 * 1. Nothing claims to be verified until `crypto.subtle.verify` said so. The
 *    default state is "unverified", and a browser that cannot do Ed25519 gets
 *    the exact `a2a receipt verify` command rather than a green tick.
 * 2. Fields the sealing observer never filled are omitted and named, not
 *    rendered as six empty rows that look like pending data.
 * 3. A token is not redacted: the previews in it are the caller's input and the
 *    agent's result, verbatim whenever the call was short. So the warning
 *    /concepts/receipts attaches to forwarding a token ships next to the copy
 *    button here, and no copy is offered without it.
 */
import { useCallback, useMemo, useState } from "react";
import type { SubagentReceipt } from "../api";
import {
  CodeBlock,
  CopyButton,
  InlineAlert,
  SurfacePanel,
  ToolbarButton,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";
import {
  groupReceiptFields,
  parseReceiptToken,
  receiptVerificationBadge,
  receiptVerificationLabel,
  receiptVerificationTone,
  receiptVerifyCommand,
  PREVIEW_LIMIT,
  RECEIPT_TOKEN_DISCLOSURE,
  SEALED_TOKEN_DISCLOSURE,
  type ReceiptFieldGroup,
  type ReceiptVerification,
} from "./receiptToken";

export function ReceiptEvidence({
  receipts,
}: {
  receipts: readonly SubagentReceipt[];
}) {
  if (receipts.length === 0) return null;
  return (
    <section className="grid min-w-0 gap-3" aria-label="Signed evidence">
      {receipts.map((artifact) =>
        artifact.kind === "receipt" ? (
          <ExecutionReceiptCard key={artifact.id} artifact={artifact} />
        ) : (
          <EvidenceArtifactCard key={artifact.id} artifact={artifact} />
        ),
      )}
    </section>
  );
}

function ExecutionReceiptCard({ artifact }: { artifact: SubagentReceipt }) {
  const token = (artifact.signed_token || "").trim();
  const parsed = useMemo(() => parseReceiptToken(token), [token]);
  const [verification, setVerification] = useState<ReceiptVerification>({
    state: "unchecked",
  });

  // The crypto + key fetch is a separate chunk: an owner who never opens a
  // receipt never downloads it.
  const check = useCallback(async () => {
    setVerification({ state: "checking" });
    try {
      const { verifyReceiptToken } = await import("./receiptVerification");
      setVerification(await verifyReceiptToken(token));
    } catch (error) {
      setVerification({
        state: "unavailable",
        reason: "no-webcrypto",
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  }, [token]);

  const receiptId = (parsed.ok ? parsed.receipt.receipt_id : artifact.receipt_id) || artifact.id;

  return (
    <SurfacePanel as="article" className="min-w-0 bg-runtime-panel/40 p-3">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-[10px] uppercase text-ink-faint">Execution receipt</span>
        <span className="min-w-0 font-mono text-xs text-ink [overflow-wrap:anywhere]">
          {receiptId}
        </span>
        {artifact.status && <StateBadge status={artifact.status} size="xs" />}
        {token && parsed.ok && (
          <StatusBadge
            tone={receiptVerificationTone(verification)}
            dot={verification.state === "valid"}
          >
            {receiptVerificationBadge(verification)}
          </StatusBadge>
        )}
      </div>

      {!token ? (
        <NoTokenNotice artifact={artifact} />
      ) : !parsed.ok ? (
        <div className="mt-3 grid gap-2">
          <InlineAlert tone="red" role="alert" className="text-xs [overflow-wrap:anywhere]">
            {parsed.error}
          </InlineAlert>
          <CopyTokenControl token={token} disclosure={SEALED_TOKEN_DISCLOSURE} />
        </div>
      ) : (
        <>
          <VerificationBar
            token={token}
            verification={verification}
            onCheck={check}
          />
          <p className="mt-3 text-[11px] leading-relaxed text-ink-muted">
            Decoded from the signed token itself — every field below is readable
            by anyone holding the token, before and without any signature check.
            The receipt carries a SHA-256 of the input plus previews of the
            input and the result capped at {PREVIEW_LIMIT} characters each,
            which for a call shorter than that is the whole thing verbatim.
          </p>
          <ReceiptFieldGroups groups={groupReceiptFields(parsed.receipt)} />
        </>
      )}

      <RawArtifactPayload artifact={artifact} />
    </SurfacePanel>
  );
}

function NoTokenNotice({ artifact }: { artifact: SubagentReceipt }) {
  const agent = artifact.agent_name || "<agent>";
  const id = artifact.receipt_id || "<receipt-id>";
  return (
    <div className="mt-3 grid gap-2">
      <InlineAlert tone="amber" className="text-xs leading-relaxed">
        This run event recorded a receipt id but no signed token, so there is
        nothing here to check. Pull the sealed receipt from the control plane
        and verify it there.
      </InlineAlert>
      <CommandBlock command={`a2a receipt verify ${id} --agent ${agent}`} />
    </div>
  );
}

function VerificationBar({
  token,
  verification,
  onCheck,
}: {
  token: string;
  verification: ReceiptVerification;
  onCheck: () => void;
}) {
  const tone = receiptVerificationTone(verification);
  return (
    <div className="mt-3 grid gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <ToolbarButton
          type="button"
          onClick={onCheck}
          disabled={verification.state === "checking"}
          variant={verification.state === "unchecked" ? "primary" : "secondary"}
          size="xs"
        >
          {verification.state === "checking" ? "Checking..." : "Check signature"}
        </ToolbarButton>
        <CopyButton value={token} label="Copy token" />
      </div>
      <TokenDisclosure text={RECEIPT_TOKEN_DISCLOSURE} />
      <InlineAlert tone={tone} className="text-xs leading-relaxed [overflow-wrap:anywhere]">
        {receiptVerificationLabel(verification)}
      </InlineAlert>
      {verification.state === "unavailable" && (
        <div className="grid gap-1">
          <span className="text-[11px] text-ink-muted">
            Check it yourself — this needs nothing from the dashboard:
          </span>
          <CommandBlock command={receiptVerifyCommand(token)} />
        </div>
      )}
      {verification.state === "valid" && (
        <p className="text-[11px] leading-relaxed text-ink-muted">
          The payload below is byte-for-byte what the platform key signed. It is
          not third-party notarization: the signing key belongs to a party to
          the transaction, and a valid signature says nothing about whether the
          answer was correct.
        </p>
      )}
    </div>
  );
}

/**
 * The non-redaction warning, rendered wherever a token can be copied. Copying
 * is the moment the previews leave the owner's screen, so the warning ships
 * with the button rather than in a paragraph somewhere above it.
 */
function TokenDisclosure({ text }: { text: string }) {
  return (
    <p className="text-[11px] leading-relaxed text-ink-muted">{text}</p>
  );
}

/** A copy button that cannot be rendered without its disclosure. */
function CopyTokenControl({ token, disclosure }: { token: string; disclosure: string }) {
  return (
    <div className="grid min-w-0 gap-1">
      <CopyButton value={token} label="Copy token" className="justify-self-start" />
      <TokenDisclosure text={disclosure} />
    </div>
  );
}

function CommandBlock({ command }: { command: string }) {
  return (
    <div className="grid min-w-0 gap-1">
      <CodeBlock className="max-h-32 p-2 text-[11px] text-ink-dim">{command}</CodeBlock>
      <CopyButton value={command} label="Copy command" className="justify-self-start" />
    </div>
  );
}

function ReceiptFieldGroups({ groups }: { groups: readonly ReceiptFieldGroup[] }) {
  return (
    <div className="mt-3 grid gap-3 border-t border-runtime-line-soft/60 pt-3 lg:grid-cols-2">
      {groups.map((group) => (
        <section key={group.id} className="min-w-0">
          <h5 className="text-[10px] uppercase text-ink-faint">{group.title}</h5>
          {group.rows.length > 0 && (
            <dl className="mt-1 grid gap-1.5">
              {group.rows.map((row) => (
                <div key={row.label} className="min-w-0">
                  <div className="flex min-w-0 items-baseline gap-2">
                    <dt className="w-20 shrink-0 text-[11px] text-ink-muted">{row.label}</dt>
                    <dd
                      className={
                        "min-w-0 flex-1 whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere] " +
                        (row.empty ? "text-ink-faint" : "text-ink-dim")
                      }
                    >
                      {row.value}
                    </dd>
                    {row.copy && <CopyButton value={row.copy} label={`Copy ${row.label}`} />}
                  </div>
                  {row.note && (
                    <p className="ml-[88px] mt-0.5 text-[10px] leading-relaxed text-ink-faint">
                      {row.note}
                    </p>
                  )}
                </div>
              ))}
            </dl>
          )}
          {group.unobserved && (
            <p className="mt-1 text-[10px] leading-relaxed text-ink-faint">
              {group.unobserved}
            </p>
          )}
        </section>
      ))}
    </div>
  );
}

function EvidenceArtifactCard({ artifact }: { artifact: SubagentReceipt }) {
  const token = (artifact.signed_token || "").trim();
  return (
    <SurfacePanel as="article" className="min-w-0 bg-runtime-panel/40 p-3">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-[10px] uppercase text-ink-faint">
          {artifact.label || artifact.kind}
        </span>
        <span className="min-w-0 font-mono text-xs text-ink [overflow-wrap:anywhere]">
          {artifact.id}
        </span>
        {artifact.status && <StateBadge status={artifact.status} size="xs" />}
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-ink-muted">
        Sealed alongside the receipt with its own schema. This view decodes
        execution receipts only, so nothing here is presented as verified.
      </p>
      {token && (
        <div className="mt-2">
          <CopyTokenControl token={token} disclosure={SEALED_TOKEN_DISCLOSURE} />
        </div>
      )}
      <RawArtifactPayload artifact={artifact} />
    </SurfacePanel>
  );
}

function RawArtifactPayload({ artifact }: { artifact: SubagentReceipt }) {
  if (!artifact.payload || Object.keys(artifact.payload).length === 0) return null;
  return (
    <details className="mt-3 min-w-0 rounded-md border border-runtime-line-soft/60 bg-runtime-bg">
      <summary className="cursor-pointer px-2 py-1 text-[11px] text-ink-muted hover:text-ink-soft focus-visible:outline focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-signal-protocol">
        Stored event payload
      </summary>
      <CodeBlock
        tabIndex={0}
        aria-label="Stored evidence event payload"
        className="max-h-48 rounded-none border-x-0 border-b-0 bg-transparent p-2 text-[11px] text-ink-muted [overflow-wrap:anywhere]"
      >
        {JSON.stringify(artifact.payload, null, 2)}
      </CodeBlock>
    </details>
  );
}
