export type ProbeState = "online" | "degraded" | "offline";

export type ServiceProbe = {
  name: string;
  url: string;
  label: string;
};

export type ServiceStatus = ServiceProbe & {
  state: ProbeState;
  code: number | null;
  latencyMs: number | null;
};

const DEFAULT_SERVICES: ServiceProbe[] = [
  {
    name: "control-plane",
    label: "Control Plane",
    url: "http://control-plane.control-plane.svc.cluster.local/healthz",
  },
  {
    name: "dashboard",
    label: "Dashboard",
    url: "http://dashboard.dashboard.svc.cluster.local/",
  },
  {
    name: "gitea",
    label: "Gitea",
    url: "http://gitea-http.gitea.svc.cluster.local:3000/",
  },
  {
    name: "langfuse",
    label: "Langfuse",
    url: "http://langfuse-web.observability.svc.cluster.local:3000/",
  },
  {
    name: "argocd",
    label: "Argo CD",
    url: "http://argocd-server.a2a-infra.svc.cluster.local/",
  },
];

async function probe(service: ServiceProbe): Promise<ServiceStatus> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2400);
  const started = Date.now();
  try {
    const response = await fetch(service.url, {
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const latencyMs = Date.now() - started;
    const state: ProbeState =
      response.status < 500 ? "online" : response.status < 600 ? "degraded" : "offline";
    return { ...service, state, code: response.status, latencyMs };
  } catch {
    return { ...service, state: "offline", code: null, latencyMs: null };
  } finally {
    clearTimeout(timeout);
  }
}

export async function getServiceStatuses(): Promise<ServiceStatus[]> {
  return Promise.all(DEFAULT_SERVICES.map(probe));
}
