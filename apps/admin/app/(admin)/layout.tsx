import Link from "next/link";
import { OnboardingWizard } from "@/components/OnboardingWizard";
import { requireAdmin } from "@/lib/auth";
import { navItems } from "@/lib/admin-data";

export const dynamic = "force-dynamic";

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const session = await requireAdmin();

  return (
    <div className="min-h-screen bg-[#08090a]">
      <header className="sticky top-0 z-20 border-b border-line bg-[#08090a]/95">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-5 py-4">
          <Link href="/" className="flex min-w-0 items-center gap-3" data-tour="admin-home">
            <div className="grid h-9 w-9 place-items-center rounded-lg border border-emerald-700/50 bg-emerald-950/40 text-sm font-semibold text-emerald-200">
              A2A
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold text-neutral-50">Admin</div>
              <div className="text-xs text-neutral-500">{session.sub}</div>
            </div>
          </Link>
          <nav className="flex max-w-full gap-1 overflow-x-auto rounded-lg border border-line bg-neutral-950 p-1">
            {navItems.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                data-tour={`nav-${item.href === "/" ? "overview" : item.href.slice(1)}`}
                className="whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-medium text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100"
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <form action="/api/admin/logout" method="post">
            <button
              type="submit"
              className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs font-medium text-neutral-200 hover:border-neutral-500 hover:text-neutral-50"
            >
              Sign out
            </button>
          </form>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-5 py-6">{children}</main>
      <OnboardingWizard />
    </div>
  );
}
