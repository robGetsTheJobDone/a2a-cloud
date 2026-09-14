import type { ReactNode } from "react";
import { ROUTE_BY_ID, type DashboardRouteId } from "../navigation";
import { PageShell, type PageShellProps } from "./DashboardChrome";

type RoutePageShellProps = Omit<
  PageShellProps,
  "eyebrow" | "title" | "description" | "maxWidth"
> & {
  routeId: DashboardRouteId;
  eyebrow?: string;
  title?: string;
  description?: ReactNode;
  maxWidth?: PageShellProps["maxWidth"];
};

export function RoutePageShell({
  routeId,
  eyebrow,
  title,
  description,
  maxWidth,
  children,
  ...pageProps
}: RoutePageShellProps) {
  const route = ROUTE_BY_ID[routeId];
  const page = route.page;

  return (
    <PageShell
      {...pageProps}
      eyebrow={eyebrow ?? page?.eyebrow ?? route.group}
      title={title ?? page?.title ?? route.label}
      description={description ?? page?.description ?? route.description}
      maxWidth={maxWidth ?? page?.maxWidth ?? "lg"}
    >
      {children}
    </PageShell>
  );
}
