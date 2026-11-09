// Argo CD application state for the dashboard, read from each cluster's Kubernetes API through its Tailscale
// API proxy with the person's own tailnet identity (no token, no new credential; the proxy maps the identity to
// a Kubernetes group). Each read first checks the cluster's kube-system UID against the expected one, so a
// renamed or swapped proxy never shows another cluster's apps.
import type { Settings } from '../shared/types';

export const ARGO = {
  platform: { api: 'https://bravo-platform-operator.tail6c7a7e.ts.net', uid: '5d978ed5-52a9-405a-a4ce-fd2e1c3ac9f9', ns: 'bravo-gitops', ui: 'https://platform-argocd.mangowhois.ai', label: '플랫폼 Argo CD' },
  customer: { api: 'https://bravo-target-operator.tail6c7a7e.ts.net', uid: '7ca7d762-ddb0-489b-9004-bab671ba2d55', ns: 'bravo-iac-gitops', ui: '', label: '고객 Argo CD · demo-chat-01' },
} as const;
export type ArgoSide = keyof typeof ARGO;

async function get(base: string, path: string) {
  // The proxy authenticates the tailnet peer; the bearer is a placeholder so kubectl-style clients skip basic auth.
  const r = await fetch(base + path, { headers: { authorization: 'Bearer unused', accept: 'application/json' }, signal: AbortSignal.timeout(10_000) });
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
  return r.json() as Promise<any>;
}

/** One Argo CD Application (Kubernetes object or Argo API item) as the dashboard shows it. */
export const mapApps = (items: any[]) => items.map((a: any) => {
  const res: any[] = a.status?.resources ?? [];
  const op = a.status?.operationState;
  return {
    name: a.metadata.name, project: a.spec?.project,
    repo: a.spec?.source?.repoURL ?? a.spec?.sources?.[0]?.repoURL, path: a.spec?.source?.path ?? a.spec?.source?.chart,
    target: a.spec?.source?.targetRevision, auto: !!a.spec?.syncPolicy?.automated && a.spec.syncPolicy.automated.enabled !== false,
    sync: a.status?.sync?.status ?? 'Unknown', revision: a.status?.sync?.revision, health: a.status?.health?.status ?? 'Unknown',
    reconciledAt: a.status?.reconciledAt, op: op ? { phase: op.phase, finishedAt: op.finishedAt, message: String(op.message ?? '').slice(0, 200) } : null,
    resources: res.length, outOfSync: res.filter((r) => r.status === 'OutOfSync').length,
    unhealthy: res.filter((r) => r.health && !['Healthy', 'Suspended'].includes(r.health.status)).length,
    conditions: (a.status?.conditions ?? []).map((x: any) => `${x.type}: ${String(x.message ?? '').slice(0, 160)}`),
  };
});

export async function argoApps(_s: Settings, side: ArgoSide) {
  const c = ARGO[side];
  try {
    const uid = (await get(c.api, '/api/v1/namespaces/kube-system')).metadata?.uid;
    if (uid !== c.uid) return { ok: false, side, label: c.label, ui: c.ui, code: 'cluster_identity_mismatch', apps: [] };
    const list = await get(c.api, `/apis/argoproj.io/v1alpha1/namespaces/${c.ns}/applications`);
    const apps = mapApps(list.items ?? []);
    return { ok: true, side, label: c.label, ui: c.ui, uid, ns: c.ns, apps };
  } catch (e) {
    const status = (e as any).status;
    return { ok: false, side, label: c.label, ui: c.ui, code: status === 403 ? 'forbidden' : status ? `http_${status}` : 'tailnet_unreachable', apps: [] };
  }
}
