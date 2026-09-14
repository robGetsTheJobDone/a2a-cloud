import { createContext, useContext, type ReactNode } from "react";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionState,
} from "../components/DashboardSectionCache";

/**
 * ChatDraftContext — hoists the workspace composer draft out of <Chat> so the
 * composer, transcript, and any future surface (mobile drawer, quick-reply
 * affordances) read and mutate one shared draft. Backed by the dashboard
 * section cache so the draft survives keep-alive route switches (mandate E).
 *
 * This is the "ChatDraftManager": the single owner of composer draft text.
 */
export type ChatDraftContextValue = {
  /** Current composer draft text for the active workspace chat. */
  composerDraft: string;
  /** Replace the composer draft. */
  setComposerDraft: (next: string) => void;
};

const ChatDraftContext = createContext<ChatDraftContextValue | null>(null);

export function ChatDraftProvider({ children }: { children: ReactNode }) {
  const [composerDraft, setComposerDraft] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.workspace.composerDraft,
    "",
  );
  return (
    <ChatDraftContext.Provider value={{ composerDraft, setComposerDraft }}>
      {children}
    </ChatDraftContext.Provider>
  );
}

/**
 * useChatDraft — read the shared composer draft. Must be called inside a
 * <ChatDraftProvider>; throws otherwise so misuse is caught at mount.
 */
export function useChatDraft(): ChatDraftContextValue {
  const ctx = useContext(ChatDraftContext);
  if (!ctx) {
    throw new Error("useChatDraft must be used within a ChatDraftProvider");
  }
  return ctx;
}
