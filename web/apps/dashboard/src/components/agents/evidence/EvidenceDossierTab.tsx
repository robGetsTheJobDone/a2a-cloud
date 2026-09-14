import type { AgentDossier } from "../../../api";
import {
  InlineAlert,
} from "../../DashboardChrome";
import { StatusBadge } from "../../StatusPillAdapters";
import { EvidenceMetric, numberValue, objectValue, textValue } from "./evidenceShared";

export function EvidenceDossierTab({
  dossier,
  quality,
  authority,
  mutation,
  risk,
}: {
  dossier: AgentDossier | null;
  quality: Record<string, unknown>;
  authority: Record<string, unknown>;
  mutation: Record<string, unknown>;
  risk: Record<string, unknown>;
}) {
  const latestReview = objectValue(quality.latest_review);
  const latestProof = objectValue(quality.latest_proof);
  return (
    <>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-6">
        <EvidenceMetric
          label="review"
          value={textValue(latestReview.status) || "none"}
          detail={textValue(latestReview.label)}
        />
        <EvidenceMetric
          label="proof"
          value={textValue(latestProof.status) || "none"}
          detail={textValue(objectValue(latestProof.payload).skill_name)}
        />
        <EvidenceMetric
          label="authority"
          value={`${numberValue(authority.grant_count)} grants`}
          detail={`${numberValue(authority.llm_usage_count)} cost rows`}
        />
        <EvidenceMetric
          label="mutation"
          value={`${numberValue(mutation.work_job_count)} jobs`}
          detail={`${numberValue(mutation.remediation_chain_count)} repairs`}
        />
        <EvidenceMetric
          label="review loops"
          value={`${numberValue(quality.active_review_loop_count)} active`}
          detail={`${numberValue(quality.review_loop_count)} total`}
        />
        <EvidenceMetric
          label="protocol sims"
          value={`${numberValue(quality.active_protocol_simulation_count)} active`}
          detail={`${numberValue(quality.protocol_simulation_count)} total`}
        />
        <EvidenceMetric
          label="trials"
          value={`${numberValue(quality.trial_count)} runs`}
          detail={`${numberValue(quality.latest_trial_score)} latest score`}
        />
        <EvidenceMetric
          label="promotion gate"
          value={`${numberValue(risk.promotion_freeze_count)} freezes`}
          detail={`${numberValue(mutation.review_loop_proposed_fix_count)} proposed fixes`}
        />
      </div>

      {dossier && dossier.warnings.length > 0 && (
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {dossier.warnings.slice(0, 4).map((warning) => (
            <InlineAlert
              key={`${warning.code}:${warning.source_ref || ""}`}
              tone="amber"
              className="text-xs"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono">{warning.code}</span>
                <StatusBadge tone="amber" className="uppercase">
                  {warning.severity}
                </StatusBadge>
              </div>
              <div className="mt-1 leading-relaxed opacity-80">
                {warning.message}
              </div>
            </InlineAlert>
          ))}
        </div>
      )}
    </>
  );
}
