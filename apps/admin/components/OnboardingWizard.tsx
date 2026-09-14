"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

type TourStep = {
  title: string;
  body: string;
  href: string;
  target: string;
};

const STORAGE_KEY = "a2a-admin-onboarding-complete";

const steps: TourStep[] = [
  {
    title: "Admin",
    body: "Start here. This console is the separate operator surface for runtime health, identity, deployments, secrets, and audit review.",
    href: "/",
    target: "admin-home",
  },
  {
    title: "Platform Status",
    body: "The overview page shows whether the core services are reachable from the admin runtime before you start deeper operations work.",
    href: "/",
    target: "platform-status",
  },
  {
    title: "Langfuse",
    body: "Langfuse is part of the stack for LLM observability and trace review, so operators can inspect model activity instead of guessing.",
    href: "/",
    target: "service-langfuse",
  },
  {
    title: "Own Gitea",
    body: "A2A runs its own Gitea. Agent source, managed repos, and build triggers stay inside the platform boundary.",
    href: "/",
    target: "service-gitea",
  },
  {
    title: "Users",
    body: "Use Users for account inventory, admin flags, owned agents, managed repositories, and the full data purge control.",
    href: "/users",
    target: "page-title",
  },
  {
    title: "Agents",
    body: "Agents covers runtime ownership, source repos, public exposure, deployment state, and cleanup work.",
    href: "/agents",
    target: "page-title",
  },
  {
    title: "Deployments",
    body: "Deployments is the release view: Argo CD reconciliation, Gitea Actions handoff, image bumps, and certificate readiness.",
    href: "/deployments",
    target: "page-title",
  },
  {
    title: "Organizations",
    body: "Organizations covers members, roles, domains, SAML, SCIM, and enterprise identity controls.",
    href: "/organizations",
    target: "page-title",
  },
  {
    title: "Platform",
    body: "Platform Settings holds global runtime switches such as the pre-deploy reviewer.",
    href: "/platform",
    target: "page-title",
  },
  {
    title: "Secrets",
    body: "Secrets is the operating checklist for admin auth, LLM keys, agent runtime secrets, repo credentials, and rotation work.",
    href: "/secrets",
    target: "page-title",
  },
  {
    title: "Audit",
    body: "Audit brings together receipts, deploy events, organization logs, and the agent work ledger.",
    href: "/audit",
    target: "page-title",
  },
];

type Highlight = {
  top: number;
  left: number;
  width: number;
  height: number;
};

export function OnboardingWizard() {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const [highlight, setHighlight] = useState<Highlight | null>(null);

  const step = steps[index];
  const currentPath = normalizePath(pathname);
  const stepPath = normalizePath(step.href);
  const onStepPage = currentPath === stepPath;

  useEffect(() => {
    if (typeof window === "undefined") return;
    setOpen(window.localStorage.getItem(STORAGE_KEY) !== "true");
  }, []);

  useEffect(() => {
    if (!open || onStepPage) return;
    router.push(step.href);
  }, [onStepPage, open, router, step.href]);

  useEffect(() => {
    if (!open) {
      setHighlight(null);
      return;
    }

    const update = () => {
      const el = document.querySelector<HTMLElement>(
        `[data-tour="${step.target}"]`,
      );
      if (!el) {
        setHighlight(null);
        return;
      }
      const rect = el.getBoundingClientRect();
      setHighlight({
        top: Math.max(8, rect.top - 6),
        left: Math.max(8, rect.left - 6),
        width: rect.width + 12,
        height: rect.height + 12,
      });
    };

    const raf = window.requestAnimationFrame(update);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open, onStepPage, step.target]);

  const progress = useMemo(
    () => Math.round(((index + 1) / steps.length) * 100),
    [index],
  );

  const close = (completed: boolean) => {
    if (completed && typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, "true");
    }
    setOpen(false);
  };

  const goTo = (nextIndex: number) => {
    const bounded = Math.min(Math.max(nextIndex, 0), steps.length - 1);
    setIndex(bounded);
    const next = steps[bounded];
    if (normalizePath(pathname) !== normalizePath(next.href)) {
      router.push(next.href);
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setIndex(0);
          setOpen(true);
          if (currentPath !== "/") router.push("/");
        }}
        className="fixed bottom-4 right-4 z-30 rounded-md border border-emerald-700/50 bg-emerald-950 px-3 py-2 text-xs font-medium text-emerald-100 shadow-2xl shadow-black/40 hover:border-emerald-500 hover:text-white"
      >
        Onboarding
      </button>

      {open && (
        <div className="pointer-events-none fixed inset-0 z-40">
          <div className="absolute inset-0 bg-black/45" />
          {highlight && (
            <div
              className="absolute rounded-lg border border-emerald-300 bg-emerald-300/5 shadow-[0_0_0_9999px_rgba(0,0,0,0.45),0_0_0_3px_rgba(16,185,129,0.18)]"
              style={{
                top: highlight.top,
                left: highlight.left,
                width: highlight.width,
                height: highlight.height,
              }}
            />
          )}

          <section className="pointer-events-auto absolute bottom-5 left-5 right-5 max-w-md rounded-lg border border-line bg-[#101214] p-4 shadow-2xl shadow-black/60 md:left-auto">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-xs uppercase text-emerald-300">
                  Step {index + 1} of {steps.length}
                </div>
                <h2 className="mt-1 text-lg font-semibold text-neutral-50">
                  {step.title}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => close(false)}
                className="rounded-md border border-neutral-700 px-2 py-1 text-xs text-neutral-300 hover:border-neutral-500 hover:text-neutral-50"
              >
                Close
              </button>
            </div>

            <p className="mt-3 text-sm leading-6 text-neutral-300">
              {step.body}
            </p>

            {!onStepPage && (
              <Link
                href={step.href}
                className="mt-3 inline-flex rounded-md border border-cyan-700/50 bg-cyan-950/30 px-3 py-2 text-xs font-medium text-cyan-100 hover:border-cyan-500"
              >
                Open {step.title}
              </Link>
            )}

            <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-neutral-900">
              <div
                className="h-full rounded-full bg-emerald-400"
                style={{ width: `${progress}%` }}
              />
            </div>

            <div className="mt-4 flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => goTo(index - 1)}
                disabled={index === 0}
                className="rounded-md border border-neutral-700 px-3 py-2 text-xs font-medium text-neutral-300 hover:border-neutral-500 hover:text-neutral-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Back
              </button>
              {index === steps.length - 1 ? (
                <button
                  type="button"
                  onClick={() => close(true)}
                  className="rounded-md border border-emerald-600 bg-emerald-900/50 px-3 py-2 text-xs font-medium text-emerald-100 hover:border-emerald-400"
                >
                  Finish
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => goTo(index + 1)}
                  className="rounded-md border border-neutral-600 bg-neutral-900 px-3 py-2 text-xs font-medium text-neutral-100 hover:border-neutral-400"
                >
                  Next
                </button>
              )}
            </div>
          </section>
        </div>
      )}
    </>
  );
}

function normalizePath(path: string): string {
  const withoutSlash = path.replace(/\/+$/, "");
  return withoutSlash || "/";
}
