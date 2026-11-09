// Human approvals go straight to the T15 approval host (bravo-approval on the tailnet) with the person's own
// AWS SSO proof for the operator audience, never through the MCP (the agent cannot approve). The host's
// certificate is self-signed for its in-cluster name, so it is pinned and that name is the TLS server name.
import { randomUUID } from 'node:crypto';
import https from 'node:https';
import { checkServerIdentity } from 'node:tls';
import type { Settings } from '../shared/types';
import { proofSource } from './proof';

const PLAN = /^plan-[0-9a-f]{32}$/;
let source: { key: string; next: () => Promise<string> } | null = null;

function proof(s: Settings) {
  const key = `${s.awsProfile}|${s.awsRegion}`;
  if (source?.key !== key) source = { key, next: proofSource(s.awsProfile, s.awsRegion, 'bravo-platform-operator') };
  return source.next();
}

function request(s: Settings, method: 'GET' | 'POST', path: string, token: string, body?: object, key?: string) {
  return new Promise<{ ok: boolean; status: number; body: any }>((resolve) => {
    const data = body ? JSON.stringify(body) : undefined;
    const req = https.request({
      host: s.approvalHost, port: 443, method, path, servername: s.approvalServerName, ca: s.approvalCert,
      // The certificate names the in-cluster Service; check it against that name, not the tailnet name.
      checkServerIdentity: (_host, cert) => checkServerIdentity(s.approvalServerName, cert),
      headers: { authorization: `Bearer ${token}`, accept: 'application/json',
        ...(data ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } : {}),
        ...(key ? { 'idempotency-key': key } : {}) },
      timeout: 20_000,
    }, (res) => {
      let raw = '';
      res.setEncoding('utf8').on('data', (c) => { if (raw.length < 1 << 20) raw += c; }).on('end', () => {
        let parsed: any = null; try { parsed = JSON.parse(raw); } catch { /* keep null */ }
        resolve({ ok: (res.statusCode ?? 0) < 300, status: res.statusCode ?? 0, body: parsed });
      });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', (e) => resolve({ ok: false, status: 0, body: { detail: { code: /CERT|self.signed|altname/i.test(e.message) ? 'approval_certificate_mismatch' : 'approval_host_unreachable' } } }));
    if (data) req.write(data);
    req.end();
  });
}

export async function approval(s: Settings, action: 'run-state' | 'approve-shift', planId: string, expectedVersion?: number) {
  if (!PLAN.test(planId)) return { ok: false, status: 400, body: { detail: { code: 'invalid_plan_id' } } };
  if (!s.approvalHost || !s.approvalCert) return { ok: false, status: 0, body: { detail: { code: 'approval_host_not_set' } } };
  let token: string;
  try { token = await proof(s); } catch { return { ok: false, status: 401, body: { detail: { code: 'sso_login_required' } } }; }
  if (action === 'run-state') return request(s, 'GET', `/v1/plans/${planId}/run-state`, token);
  if (!Number.isInteger(expectedVersion) || expectedVersion! < 1) return { ok: false, status: 400, body: { detail: { code: 'invalid_version' } } };
  // SHIFT approval binds the stored GATE checkpoint at this exact plan version; valid for 30 minutes.
  const expires = new Date(Date.now() + 30 * 60_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  return request(s, 'POST', `/v1/plans/${planId}/approvals`, token,
    { approval_kind: 'shift', expected_version: expectedVersion, expires_at: expires }, `console-shift-${planId.slice(5, 17)}-v${expectedVersion}-${randomUUID().slice(0, 8)}`);
}
