export type ThreadContentLoadState = {
  threadId: string | null;
  streaming: boolean;
  localStreamThreadId: string | null;
};

export function shouldDeferThreadContentLoad({
  threadId,
  streaming,
  localStreamThreadId,
}: ThreadContentLoadState): boolean {
  return Boolean(
    streaming &&
      threadId &&
      localStreamThreadId &&
      threadId === localStreamThreadId,
  );
}
