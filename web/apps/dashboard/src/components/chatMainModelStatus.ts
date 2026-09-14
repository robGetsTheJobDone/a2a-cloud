import type { LLMCreds } from "../api";

export type ChatMainModelStatus = {
  credential: LLMCreds | null;
  requestCredsName?: string;
  canChat: boolean;
  loading: boolean;
  label: string;
  blocker: string | null;
  placeholder: string;
};

function preferredChatMainCredential(
  credsList: readonly LLMCreds[],
  llmCredsName: string,
): LLMCreds | null {
  if (llmCredsName) {
    const selected = credsList.find((cred) => cred.name === llmCredsName);
    if (selected) return selected;
  }
  return (
    credsList.find((cred) => cred.name === "default") ??
    credsList[0] ??
    null
  );
}

function requestCredsName(credential: LLMCreds | null): string | undefined {
  if (!credential || credential.name === "default") return undefined;
  return credential.name;
}

export function resolveChatMainModelStatus(
  credsList: readonly LLMCreds[],
  llmCredsName: string,
  credsLoaded: boolean,
): ChatMainModelStatus {
  if (!credsLoaded) {
    return {
      credential: null,
      canChat: false,
      loading: true,
      label: "checking LLM keys",
      blocker: "Checking saved LLM keys before starting chat.",
      placeholder: "Loading LLM keys...",
    };
  }

  const credential = preferredChatMainCredential(credsList, llmCredsName);
  if (!credential) {
    return {
      credential: null,
      canChat: false,
      loading: false,
      label: "LLM key required",
      blocker: "Add an LLM key before starting chat.",
      placeholder: "Add an LLM key before chatting.",
    };
  }

  return {
    credential,
    requestCredsName: requestCredsName(credential),
    canChat: true,
    loading: false,
    label: `${credential.model} · ${
      credential.name === "default" ? "default key" : credential.name
    }`,
    blocker: null,
    placeholder: "Ask the main agent. Drop files here or paste screenshots.",
  };
}
