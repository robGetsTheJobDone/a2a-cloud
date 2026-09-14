import { CollectiveRuntimeToggle } from "@/components/CollectiveRuntimeToggle";
import { ReviewerToggle } from "@/components/ReviewerToggle";
import { listPlatformSettings } from "@/lib/cp";

export const dynamic = "force-dynamic";

export default async function PlatformPage() {
  let settings: Awaited<ReturnType<typeof listPlatformSettings>> = [];
  let loadError: string | null = null;
  try {
    settings = await listPlatformSettings();
  } catch (e) {
    loadError = e instanceof Error ? e.message : String(e);
  }
  const reviewer =
    settings.find((s) => s.key === "reviewer_enabled") || null;
  const reviewerValue = reviewer ? Boolean(reviewer.value) : true;
  const collectiveRuntime =
    settings.find((s) => s.key === "collective_runtime_enabled") || null;
  const collectiveRuntimeValue = collectiveRuntime ? Boolean(collectiveRuntime.value) : true;

  return (
    <div className="space-y-6">
      <header className="border-b border-line pb-5" data-tour="page-title">
        <div className="text-xs uppercase text-neutral-500">Runtime</div>
        <h1 className="mt-2 text-3xl font-semibold text-neutral-50">
          Platform Settings
        </h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-neutral-400">
          Global toggles backing the control plane. Changes take effect
          immediately on the next async job; existing in-flight tasks finish
          under the prior setting.
        </p>
      </header>

      {loadError && (
        <div className="rounded-md border border-red-800/60 bg-red-950/30 px-4 py-3 text-sm text-red-200">
          Could not reach the control plane: {loadError}
        </div>
      )}

      <CollectiveRuntimeToggle
        initialValue={collectiveRuntimeValue}
        updatedBy={collectiveRuntime?.updated_by ?? null}
        updatedAt={collectiveRuntime?.updated_at ?? null}
      />

      <ReviewerToggle
        initialValue={reviewerValue}
        updatedBy={reviewer?.updated_by ?? null}
        updatedAt={reviewer?.updated_at ?? null}
      />
    </div>
  );
}
