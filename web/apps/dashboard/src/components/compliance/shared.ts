import {
  type ComplianceDecisionRecordKind,
  type ComplianceFramework,
  type ComplianceRiskTier,
  type ComplianceSignatureStatus,
  type ComplianceStatus,
} from "../../api";
import { type StatusBadgeTone } from "../DashboardChrome";
import { type ComplianceViewId } from "../../navigation";

export const COMPLIANCE_FRAMEWORKS: ReadonlyArray<{
  id: ComplianceFramework;
  label: string;
  description: string;
}> = [
  {
    id: "eu_ai_act",
    label: "EU AI Act",
    description: "Union-wide AI regulation. Requires at least 180 days of decision-record retention.",
  },
  {
    id: "nist_ai_rmf_1_1",
    label: "NIST AI RMF 1.1",
    description: "US voluntary AI risk management framework.",
  },
  {
    id: "iso_42001",
    label: "ISO/IEC 42001",
    description: "AI management system standard for certifiable governance.",
  },
];

export const EU_AI_ACT_RETENTION_FLOOR_DAYS = 180;

export const DECISION_RECORD_KINDS: ReadonlyArray<{
  id: ComplianceDecisionRecordKind;
  label: string;
}> = [
  { id: "skill_execution", label: "Skill execution" },
  { id: "authorization", label: "Authorization" },
  { id: "admin_action", label: "Admin action" },
];

export const COMPLIANCE_RISK_TIERS: ReadonlyArray<{
  id: ComplianceRiskTier;
  label: string;
}> = [
  { id: "unclassified", label: "Unclassified" },
  { id: "minimal", label: "Minimal" },
  { id: "limited", label: "Limited" },
  { id: "high", label: "High" },
  { id: "unacceptable", label: "Unacceptable" },
];

export function complianceRoute(slug: string, view: ComplianceViewId = "records") {
  const base = `/compliance/${encodeURIComponent(slug)}`;
  return view === "records" ? base : `${base}/${view}`;
}

export function decisionRecordKindLabel(kind: string) {
  return DECISION_RECORD_KINDS.find((entry) => entry.id === kind)?.label ?? kind;
}

export function riskTierLabel(tier: string) {
  return COMPLIANCE_RISK_TIERS.find((entry) => entry.id === tier)?.label ?? tier;
}

export function riskTierTone(tier: ComplianceRiskTier): StatusBadgeTone {
  if (tier === "unacceptable") return "red";
  if (tier === "high") return "amber";
  if (tier === "unclassified") return "neutral";
  return "emerald";
}

export function signatureStatusTone(
  status: ComplianceSignatureStatus,
): StatusBadgeTone {
  if (status === "valid") return "emerald";
  if (status === "invalid") return "red";
  return "neutral";
}

export function signatureStatusLabel(status: ComplianceSignatureStatus) {
  if (status === "valid") return "signature valid";
  if (status === "invalid") return "signature invalid";
  return "not applicable";
}

export function outcomeTone(outcome: string): StatusBadgeTone {
  const normalized = outcome.toLowerCase();
  if (["denied", "failed", "failure", "error", "rejected"].includes(normalized)) {
    return "red";
  }
  if (["allowed", "approved", "ok", "success", "succeeded", "completed"].includes(normalized)) {
    return "emerald";
  }
  return "neutral";
}

export function compliancePosture(status: ComplianceStatus): {
  tone: StatusBadgeTone;
  label: string;
  detail: string;
} {
  const unclassified = status.agents_total - status.agents_classified;
  if (status.policy.legal_hold) {
    return {
      tone: "amber",
      label: "legal hold",
      detail: status.policy.legal_hold_reason
        ? `Legal hold active: ${status.policy.legal_hold_reason}`
        : "Legal hold active — retention enforcement is suspended.",
    };
  }
  if (!status.retention_ok) {
    return {
      tone: "red",
      label: "retention gap",
      detail: "The retention window does not satisfy the active frameworks.",
    };
  }
  if (!status.policy.persisted) {
    return {
      tone: "amber",
      label: "policy not saved",
      detail: "The compliance policy is running on defaults — save it to make it binding.",
    };
  }
  if (unclassified > 0) {
    return {
      tone: "amber",
      label: "classification gap",
      detail: `${unclassified.toLocaleString()} agent${unclassified === 1 ? "" : "s"} still need a risk-tier classification.`,
    };
  }
  return {
    tone: "emerald",
    label: "ready",
    detail: "Retention, policy, and agent classification are in place for the enforcement date.",
  };
}

export function totalRecordCount(status: ComplianceStatus) {
  const counts = status.record_counts;
  return counts.skill_execution + counts.authorization + counts.admin_action;
}

export function formatComplianceDate(value: string | null | undefined) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatComplianceDay(value: string | null | undefined) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Convert a `<input type="date">` value to an ISO instant, or undefined. */
export function dateInputToIso(value: string, endOfDay = false): string | undefined {
  if (!value) return undefined;
  const iso = `${value}T${endOfDay ? "23:59:59.999" : "00:00:00.000"}Z`;
  return Number.isNaN(new Date(iso).getTime()) ? undefined : iso;
}

export function downloadJsonFile(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function evidencePackFilename(slug: string) {
  const day = new Date().toISOString().slice(0, 10);
  return `a2a-evidence-pack-${slug}-${day}.json`;
}
