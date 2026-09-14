import Link from "next/link";
import { redirect } from "next/navigation";
import { getAdminSession, adminAuthConfigured } from "@/lib/auth";

export const dynamic = "force-dynamic";

const errorCopy: Record<string, string> = {
  config: "Admin Keycloak auth is not configured.",
  denied: "Keycloak sign-in was cancelled.",
  state: "The sign-in session expired. Try again.",
  token: "Keycloak token exchange failed.",
  forbidden: "This account is not marked as an admin.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams?: Promise<{ error?: string }>;
}) {
  const session = await getAdminSession();
  if (session) redirect("/");
  const params = searchParams ? await searchParams : {};
  const error = params.error;
  const configured = adminAuthConfigured();

  return (
    <main className="grid min-h-screen place-items-center px-5 py-10">
      <div className="w-full max-w-sm rounded-lg border border-line bg-panel p-6 shadow-2xl shadow-black/30">
        <div className="grid h-10 w-10 place-items-center rounded-lg border border-emerald-700/50 bg-emerald-950/40 text-sm font-semibold text-emerald-200">
          A2A
        </div>
        <h1 className="mt-5 text-2xl font-semibold text-neutral-50">Admin sign in</h1>
        <p className="mt-2 text-sm leading-6 text-neutral-500">
          Use your A2A Cloud Keycloak account.
        </p>

        {error && (
          <div className="mt-5 rounded-md border border-red-800/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
            {errorCopy[error] || "Admin sign-in failed."}
          </div>
        )}
        {!configured && (
          <div className="mt-5 rounded-md border border-amber-700/50 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
            Missing admin session secret or control-plane admin token.
          </div>
        )}

        <Link
          href="/api/admin/login"
          aria-disabled={!configured}
          className={`mt-5 block w-full rounded-md bg-emerald-500 px-3 py-2 text-center text-sm font-semibold text-neutral-950 hover:bg-emerald-400 ${
            configured ? "" : "pointer-events-none opacity-50"
          }`}
        >
          Sign in with Keycloak
        </Link>
      </div>
    </main>
  );
}
