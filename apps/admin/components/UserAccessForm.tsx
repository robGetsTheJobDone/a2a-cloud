"use client";

import { useState } from "react";

type Props = {
  userId: number;
};

export function UserAccessForm({ userId }: Props) {
  const [tokenBusy, setTokenBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);

  async function mintToken() {
    setTokenBusy(true);
    setStatus(null);
    setToken(null);
    try {
      const resp = await fetch(`/api/admin/users/${userId}/platform-token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ttl_seconds: 604800,
          label: "admin route testing",
        }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const out = (await resp.json()) as { token: string; expires_at: string };
      setToken(out.token);
      setExpiresAt(out.expires_at);
      setStatus("Token minted");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setTokenBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={tokenBusy}
          onClick={mintToken}
          className="h-8 rounded-md border border-neutral-700 bg-neutral-900 px-3 text-xs text-neutral-100 disabled:opacity-50"
        >
          Mint token
        </button>
      </div>
      {status && <div className="truncate text-xs text-neutral-500">{status}</div>}
      {token && (
        <div className="space-y-1">
          <textarea
            readOnly
            value={token}
            className="h-20 w-full resize-none rounded-md border border-line bg-neutral-950 p-2 font-mono text-[11px] text-neutral-200 outline-none"
          />
          {expiresAt && (
            <div className="text-[11px] text-neutral-500">
              Expires {new Date(expiresAt).toLocaleString()}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
