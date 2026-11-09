// window.sova for the browser: the same API the desktop preload exposes, over this site's /api.
// Approvals and the customer cluster's Argo need the person's own SSO / tailnet identity: desktop only.
import type { AgentEvent, CallOutcome, EventPush, SovaApi } from '../shared/types';

const HEADERS = { 'content-type': 'application/json', 'x-sova-console': '1' };

async function req<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, { method, headers: method === 'POST' ? HEADERS : undefined, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  // An expired Cloudflare Access session: reload so Access signs the person in again.
  if (r.status === 401) { location.reload(); throw new Error('access_required'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data?.detail?.code ?? `http_${r.status}`), { code: data?.detail?.code ?? `http_${r.status}` });
  return data as T;
}
const get = <T>(path: string) => req<T>('GET', path);
const post = <T>(path: string, body: unknown) => req<T>('POST', path, body);

let agentStream: EventSource | null = null;
const agentListeners = new Set<(e: AgentEvent) => void>();
function agentEvents() {
  if (agentStream) return;
  agentStream = new EventSource('/api/stream', { withCredentials: true });
  agentStream.addEventListener('agent', (m) => { const e = JSON.parse((m as MessageEvent).data); agentListeners.forEach((cb) => cb(e)); });
}

const desktopOnly = { ok: false, status: 0, body: { detail: { code: 'desktop_only' } } };

const api: SovaApi & { web: true } = {
  web: true,
  call: async (name, args) => {
    try { return await post<CallOutcome>('/api/call', { name, args }); } catch (e) { return { ok: false, code: (e as { code?: string }).code ?? 'mcp_unreachable' }; }
  },
  status: () => get('/api/status'),
  resource: (uri) => post('/api/resource', { uri }),
  settings: () => get('/api/settings'),
  saveSettings: (patch) => post('/api/settings', patch),
  ssoLogin: async () => ({ ok: true, message: '웹에서는 Cloudflare Access 로그인이 신원입니다.' }),
  agentSend: async (conv, text, opts) => { agentEvents(); await post('/api/agent/send', { conv, text, opts }); },
  prompts: () => get('/api/prompts'),
  models: () => get('/api/models'),
  agentAuth: () => get('/api/agent/auth'),
  openaiKey: async () => {
    const a = await get<{ ok: boolean }>('/api/agent/auth');
    return { ok: false, message: '웹에서는 플랫폼에 등록된 키를 씁니다.', configured: a.ok, last4: null, encrypted: true };
  },
  argo: async (side) => side === 'platform' ? get('/api/argo/platform')
    : { ok: false, side, label: '고객 Argo CD · demo-chat-01', ui: '', code: 'desktop_only', apps: [] },
  approval: async () => desktopOnly,
  awsProfile: async () => ({}),
  prompt: (name, args) => post('/api/prompt', { name, args }),
  agentStop: async (conv) => { await post('/api/agent/stop', { conv }); },
  agentConfirm: async (id, allow) => { await post('/api/agent/confirm', { id, allow }); },
  onAgent: (cb) => { agentEvents(); agentListeners.add(cb); return () => { agentListeners.delete(cb); }; },
  onDeepLink: () => () => undefined,
  watchEvents: (planId, cb) => {
    // The server stamps each push with the plan's last event id, so a reconnect resumes from it.
    const es = new EventSource(`/api/plans/${encodeURIComponent(planId)}/events`, { withCredentials: true });
    es.onmessage = (m) => { const p = JSON.parse(m.data) as EventPush; if (p.mode === 'unsupported') es.close(); cb(p); };
    es.onerror = () => { if (es.readyState !== EventSource.CLOSED) cb({ mode: 'retrying', code: 'stream_closed' }); };
    return () => es.close();
  },
};

(window as unknown as { sova: typeof api }).sova = api;
