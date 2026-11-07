// The app's only path to SOVA: a loopback proxy that adds a fresh SSO proof to each request
// and forwards to the platform MCP. The dashboard (MCP client here) and the agent (Agent SDK
// http server config) both go through it, so both see the same tools and server checks.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { randomBytes } from 'node:crypto';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { CallOutcome, LinkStatus, PromptInfo, Settings } from '../shared/types';
import { proofSource } from './proof';
import { isLoopback } from './settings';

const MAX_BODY = 1 << 20;
const WRITES = new Set(['sova_register_environment', 'sova_register_workload', 'sova_create_plan']);

/** A confirming state-making call (second step) that must wait for the person's click. */
export function confirmRequest(body: Buffer): { name: string; args: Record<string, unknown> } | null {
  try {
    const msg = JSON.parse(body.toString('utf8'));
    const name = msg?.params?.name, args = msg?.params?.arguments;
    return msg?.method === 'tools/call' && WRITES.has(name) && args?.confirmation ? { name, args } : null;
  } catch { return null; }
}

export class SovaLink {
  readonly secret = randomBytes(32).toString('hex');
  private server?: http.Server;
  private client?: Client;
  private proof!: () => Promise<string>;
  private lastCode?: string;
  /** Set by the host: asks the person; false or no host means the write is refused. */
  confirm?: (name: string, args: Record<string, unknown>) => Promise<boolean>;

  constructor(private settings: () => Settings) {}

  reset() {
    const s = this.settings();
    const source = proofSource(s.awsProfile, s.awsRegion);
    this.proof = s.devToken && isLoopback(s.mcpUrl) ? async () => s.devToken : source;
    void this.client?.close().catch(() => undefined);
    this.client = undefined;
  }

  get url() { return `http://127.0.0.1:${(this.server?.address() as AddressInfo).port}/mcp`; }

  async start() {
    this.server = http.createServer((req, res) => void this.forward(req, res));
    await new Promise<void>((ok) => this.server!.listen(0, '127.0.0.1', ok));
  }

  private upstream() {
    const s = this.settings();
    const url = new URL(s.mcpUrl);
    url.searchParams.set('read_only', String(s.readOnly));
    if (s.planScope) url.searchParams.set('plan', s.planScope);
    return url;
  }

  private async forward(req: http.IncomingMessage, res: http.ServerResponse) {
    const fail = (status: number, code: string) => {
      this.lastCode = code;
      res.writeHead(status, { 'content-type': 'application/json' }).end(JSON.stringify({ detail: { code } }));
    };
    if (req.headers.authorization !== `Bearer ${this.secret}`) return fail(401, 'local_secret_required');
    if (!req.url?.startsWith('/mcp')) return fail(404, 'not_found');
    if (!this.settings().mcpUrl) return fail(503, 'mcp_url_not_set');
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const c of req) { size += c.length; if (size > MAX_BODY) return fail(413, 'too_large'); chunks.push(c); }
    const write = chunks.length ? confirmRequest(Buffer.concat(chunks)) : null;
    if (write && !(await this.confirm?.(write.name, write.args))) {
      const id = JSON.parse(Buffer.concat(chunks).toString('utf8')).id;
      const text = JSON.stringify({ detail: { code: 'user_declined' }, hint: '사람이 확인 창에서 등록을 승인하지 않았습니다.' });
      res.writeHead(200, { 'content-type': 'application/json' })
        .end(JSON.stringify({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text }], isError: true } }));
      return;
    }
    let token: string;
    try { token = await this.proof(); } catch { return fail(401, 'sso_login_required'); }
    const headers: Record<string, string> = { authorization: `Bearer ${token}` };
    for (const h of ['content-type', 'accept', 'mcp-protocol-version', 'mcp-session-id', 'last-event-id']) {
      const v = req.headers[h]; if (typeof v === 'string') headers[h] = v;
    }
    try {
      const send = () => fetch(this.upstream(), {
        method: req.method, headers, body: chunks.length ? Buffer.concat(chunks) : undefined,
        redirect: 'manual', signal: AbortSignal.timeout(30_000),
      });
      // Retry only what never ran: a 503 identity_unavailable is refused before any tool (STS check timed out),
      // and a refused connection (pod restarting) never reached the server. A broken read may also retry.
      let up: Response | undefined;
      for (const wait of [0, 600, 1800]) {
        if (wait) await new Promise((r) => setTimeout(r, wait));
        try { up = await send(); } catch (e) {
          if (wait === 1800 || (write && !String((e as any)?.cause?.code).startsWith('ECONNREFUSED'))) throw e;
          continue;
        }
        if (up.status !== 503 || wait === 1800) break;
        const code = await up.clone().json().then((j) => j?.detail?.code, () => undefined);
        if (code !== 'identity_unavailable') break;
      }
      if (!up) throw new Error('unreachable');
      this.lastCode = up.ok ? undefined : `upstream_${up.status}`;
      const out: Record<string, string> = {};
      for (const h of ['content-type', 'mcp-session-id', 'www-authenticate']) { const v = up.headers.get(h); if (v) out[h] = v; }
      res.writeHead(up.status, out);
      if (up.body) for await (const c of up.body as any) res.write(c);
      res.end();
    } catch {
      // Headers may already be out (a streamed body broke); then only the connection can end.
      if (res.headersSent) res.destroy(); else fail(502, 'mcp_unreachable');
    }
  }

  private async connect() {
    if (this.client) return this.client;
    const client = new Client({ name: 'sova-console', version: '0.1.0' });
    await client.connect(new StreamableHTTPClientTransport(new URL(this.url), {
      requestInit: { headers: { Authorization: `Bearer ${this.secret}` } },
    }));
    this.client = client;
    return client;
  }

  async call(name: string, args: Record<string, unknown> = {}): Promise<CallOutcome> {
    try {
      const r = await (await this.connect()).callTool({ name, arguments: args });
      if (r.isError) {
        const text: string = (r.content as any[])?.[0]?.text ?? '';
        // The SDK prefixes "Error executing tool <name>: " to the server's JSON detail.
        try { const j = JSON.parse(text.slice(text.indexOf('{'))); return { ok: false, code: j.detail?.code ?? 'tool_error', hint: j.hint ?? undefined }; }
        catch { return { ok: false, code: 'tool_error', hint: text.slice(0, 300) }; }
      }
      return { ok: true, result: r.structuredContent as any };
    } catch {
      this.client = undefined;
      return { ok: false, code: this.lastCode ?? 'mcp_unreachable' };
    }
  }

  async prompts(): Promise<PromptInfo[]> {
    try {
      return (await (await this.connect()).listPrompts()).prompts.map((p) => ({
        name: p.name, title: p.title, description: p.description,
        args: (p.arguments ?? []).map((a) => ({ name: a.name, required: a.required })),
      }));
    } catch { this.client = undefined; return []; }
  }

  async prompt(name: string, args: Record<string, string>): Promise<string | null> {
    try {
      const r = await (await this.connect()).getPrompt({ name, arguments: args });
      return r.messages.map((m) => (m.content.type === 'text' ? m.content.text : '')).join('\n') || null;
    } catch { this.client = undefined; return null; }
  }

  async resource(uri: string): Promise<string | null> {
    try {
      const r = await (await this.connect()).readResource({ uri });
      return (r.contents[0] as any)?.text ?? null;
    } catch { this.client = undefined; return null; }
  }

  async status(): Promise<LinkStatus> {
    const s = this.settings();
    const base = { url: s.mcpUrl, readOnly: s.readOnly, checkedAt: new Date().toISOString() };
    try {
      const tools = (await (await this.connect()).listTools()).tools.map((t) => t.name);
      return { ...base, connected: true, tools };
    } catch {
      this.client = undefined;
      return { ...base, connected: false, tools: [], code: this.lastCode ?? (s.mcpUrl ? 'mcp_unreachable' : 'mcp_url_not_set') };
    }
  }
}
