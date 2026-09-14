/**
 * NewBountySheet — the "post a bounty" surface, kept DISTINCT from the bounty
 * detail sheet (mandate C: create + detail are separate in-place right-side
 * sheets layered over a list that never unmounts).
 *
 * Owns the create form state and the DetailSheet shell together so the draft
 * survives re-renders of the parent list while the sheet is open. The form is
 * only mounted while `open` is true (route-driven), matching the prior detail
 * sheet behavior. A successful post stays in the sheet long enough to recruit
 * builders instead of discarding the highest-intent sharing moment.
 */
import { useId, useState, type FormEvent } from "react";
import { createBounty, type Bounty } from "../api";
import { trackEvent } from "../analytics";
import {
  FormField,
  InlineAlert,
  SurfacePanel,
  TextArea,
  TextInput,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { DetailSheet } from "./ListDetailLayout";

export type NewBountySheetProps = {
  open: boolean;
  onClose: () => void;
  onCreated: (bounty: Bounty) => void | Promise<void>;
};

export function NewBountySheet({ open, onClose, onCreated }: NewBountySheetProps) {
  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      title="Post a bounty"
      description="Describe the work and acceptance examples for agent authors."
      size="lg"
    >
      {/* Mounted only while open so a fresh draft starts each time the sheet
          opens; the list behind it stays mounted regardless (mandate C/E). */}
      {open && <NewBountyForm onCancel={onClose} onCreated={onCreated} />}
    </DetailSheet>
  );
}

function NewBountyForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (bounty: Bounty) => void | Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [exampleInput, setExampleInput] = useState("");
  const [exampleOutput, setExampleOutput] = useState("");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<Bounty | null>(null);
  const formId = useId();

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setErr(null);

    setBusy(true);
    try {
      const bounty = await createBounty({
        title: title.trim(),
        description: description.trim(),
        example_input: exampleInput.trim(),
        example_output: exampleOutput.trim(),
        tags: tags
          .split(",")
          .map((t) => t.trim().toLowerCase())
          .filter(Boolean),
      });
      setCreated(bounty);
      trackEvent("bounty_created", {
        bountyId: bounty.id,
        bountySlug: bounty.slug,
        tagCount: bounty.tags.length,
      });
      await onCreated(bounty);
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    return <BountyShareSuccess bounty={created} onDone={onCancel} />;
  }

  return (
    <section className="min-w-0">
      <div className="sticky top-0 z-10 -mx-px flex shrink-0 flex-wrap justify-end gap-2 border-b border-runtime-line-soft/60 bg-runtime-bg/95 pb-3 backdrop-blur">
        <ToolbarButton type="button" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </ToolbarButton>
        <ToolbarButton
          form={formId}
          type="submit"
          variant="primary"
          disabled={busy}
          aria-busy={busy}
        >
          {busy ? "Posting..." : "Post bounty"}
        </ToolbarButton>
      </div>

      <form
        id={formId}
        onSubmit={handleSubmit}
        className="mt-4 space-y-4"
        aria-busy={busy}
      >
        <FormField label="Title" description="What do you need an agent to do?">
          <TextInput
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={160}
            placeholder="Summarize daily emails by importance"
            disabled={busy}
          />
        </FormField>
        <FormField
          label="Description"
          description="Brief, the agent author reads this first."
        >
          <TextArea
            required
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={5}
            maxLength={8000}
            disabled={busy}
          />
        </FormField>
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <FormField label="Example input" description="What the agent receives.">
            <TextArea
              value={exampleInput}
              onChange={(e) => setExampleInput(e.target.value)}
              rows={4}
              maxLength={4000}
              disabled={busy}
              mono
            />
          </FormField>
          <FormField label="Example output" description="What you'd accept back.">
            <TextArea
              value={exampleOutput}
              onChange={(e) => setExampleOutput(e.target.value)}
              rows={4}
              maxLength={4000}
              disabled={busy}
              mono
            />
          </FormField>
        </div>
        <div className="grid min-w-0 gap-3">
          <FormField label="Tags" description="Comma separated.">
            <TextInput
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="email, summary"
              disabled={busy}
            />
          </FormField>
        </div>

        {err && (
          <InlineAlert tone="red" role="alert">
            <span className="break-words">{err}</span>
          </InlineAlert>
        )}
      </form>
    </section>
  );
}

export function bountyShareUrl(slug: string, channel: string): string {
  const origin = globalThis.location?.origin || "http://localhost";
  const url = new URL(`/bounties/${encodeURIComponent(slug)}`, origin);
  url.searchParams.set("utm_source", "bounty_poster");
  url.searchParams.set("utm_medium", channel);
  url.searchParams.set("utm_campaign", "bounty_recruitment");
  url.searchParams.set("utm_content", slug);
  url.searchParams.set("utm_id", `bounty:${slug}`);
  return url.toString();
}

function BountyShareSuccess({ bounty, onDone }: { bounty: Bounty; onDone: () => void }) {
  const [notice, setNotice] = useState<string | null>(null);
  const publicUrl = bountyShareUrl(bounty.slug, "share");
  const shareText = `Know someone who can build this? “${bounty.title}” is open on a2a cloud.`;
  const xUrl = new URL("https://x.com/intent/post");
  xUrl.searchParams.set("text", `${shareText}\n\n${bountyShareUrl(bounty.slug, "social")}`);

  async function share() {
    trackEvent("bounty_share_started", { bountyId: bounty.id, bountySlug: bounty.slug });
    if (navigator.share) {
      try {
        await navigator.share({
          title: `${bounty.title} — agent bounty`,
          text: shareText,
          url: publicUrl,
        });
        setNotice("Bounty shared.");
        trackEvent("bounty_share_completed", {
          bountyId: bounty.id,
          bountySlug: bounty.slug,
          channel: "native",
        });
        return;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
      }
    }
    if (await copyText(bountyShareUrl(bounty.slug, "copy"))) {
      setNotice("Recruiting link copied.");
      trackEvent("bounty_share_completed", {
        bountyId: bounty.id,
        bountySlug: bounty.slug,
        channel: "copy",
      });
    }
  }

  return (
    <section className="space-y-5">
      <InlineAlert tone="emerald" role="status">
        Your bounty is live. The fastest route to a strong claim is to send it to builders now.
      </InlineAlert>

      <SurfacePanel as="div" className="bg-runtime-bg p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
              live bounty
            </div>
            <h3 className="mt-1 text-base font-semibold text-ink">{bounty.title}</h3>
            <p className="mt-2 max-w-2xl text-sm text-ink-dim">
              Share the public brief. Builders land on the exact request, then sign in to claim it
              with a deployed agent.
            </p>
          </div>
        </div>
      </SurfacePanel>

      <div className="flex flex-wrap gap-2">
        <ToolbarButton type="button" variant="primary" size="md" onClick={() => void share()}>
          Share with builders
        </ToolbarButton>
        <ToolbarLink
          href={xUrl.toString()}
          external
          size="md"
          onClick={() =>
            trackEvent("bounty_share_completed", {
              bountyId: bounty.id,
              bountySlug: bounty.slug,
              channel: "x",
            })
          }
        >
          Post on X
        </ToolbarLink>
        <ToolbarLink href={`/bounties/mine/${encodeURIComponent(bounty.slug)}`} size="md">
          Open bounty
        </ToolbarLink>
        <ToolbarButton type="button" variant="ghost" size="md" onClick={onDone}>
          Done
        </ToolbarButton>
      </div>

      {notice && (
        <p className="text-xs text-signal-live" role="status" aria-live="polite">
          {notice}
        </p>
      )}
    </section>
  );
}

async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fall through to selection copy.
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    return copied;
  } catch {
    return false;
  }
}

function messageFromError(ex: unknown): string {
  return ex instanceof Error ? ex.message : String(ex);
}
