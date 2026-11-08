// SOVA Console on the web (sova.mangowhois.ai). The desktop renderer runs unchanged in the browser; the desktop
// main-process logic (MCP link, guide, plan event streams) runs here, once per person. Who the person is comes
// only from their Cloudflare Access token, which the SOVA MCP verifies again on every call, so registrations and
// reads stay attributed to that person. Human approvals and the customer cluster's Argo need the person's own
// SSO / tailnet identity and stay in the desktop app. The model key is the platform's (mounted file), never the person's.
import { createHash } from 'node:crypto';
import { createReadStream, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { extname, join, normalize, sep } from 'node:path';
import type { AgentEvent, EventPush, Settings, TurnOptions } from '../shared/types';
import { Agents } from '../main/agent';
import { mapApps } from '../main/argo';
import { followPlan } from '../main/events';
import { codexModels } from '../main/models';
import { SovaLink } from '../main/sova';
import { AccessVerifier } from './access';

const need = (k: string, fallback?: string) => {
  const v = process.env[k] ?? fallback;
  if (v === undefined || v === '') throw new Error(`missing_env:${k}`);
  return v;
};
const CFG = {
  port: Number(process.env.PORT ?? 8080),
  origin: need('PUBLIC_ORIGIN'),                         // https://sova.mangowhois.ai
  mcpUrl: need('MCP_URL'),                               // in-cluster SOVA MCP, …/mcp
  team: need('ACCESS_TEAM_DOMAIN'),                      // <team>.cloudflareaccess.com
  aud: process.env.ACCESS_AUD ?? '',                     // Access application audience tag
  keyFile: process.env.OPENAI_KEY_FILE ?? '/var/run/secrets/sova-console/openai/api_key',
  data: process.env.DATA_DIR ?? '/data',
  statics: process.env.STATIC_DIR ?? join(__dirname, '..', 'web'),
  codexBin: need('CODEX_BIN'),
  codexPath: process.env.CODEX_PATH ?? '',
  argo: process.env.ARGO_PLATFORM_URL ?? 'https://platform-argocd.mangowhois.ai',
};
const verifier = new AccessVerifier({ team: CFG.team, aud: CFG.aud });

const readKey = () => { try { const k = readFileSync(CFG.keyFile, 'utf8').trim(); return k || null; } catch { return null; } };

/* ---------- one set of desktop main-process objects per person ---------- */

interface Person {
  id: string; email: string; token: string; exp: number; dir: string; settings: Settings;
  link: SovaLink; agents: Agents; listeners: Set<http.ServerResponse>; seq: number; backlog: { id: number; line: string }[];
}
const people = new Map<string, Person>();

const DEFAULTS = (): Settings => ({
  mcpUrl: CFG.mcpUrl, awsProfile: '', awsRegion: '', readOnly: true, planScope: '', model: '', devToken: '', onboarded: false,
  gcpProject: '', tailnet: '', agentAuth: 'apikey', approvalHost: '', approvalServerName: '', approvalCert: '',
});
// Only these follow the person; the MCP address, identity and model billing are the server's.
const PERSONAL = ['readOnly', 'planScope', 'model', 'onboarded', 'gcpProject', 'tailnet'] as const;

function loadSettings(dir: string): Settings {
  try {
    const saved = JSON.parse(readFileSync(join(dir, 'settings.json'), 'utf8'));
    return { ...DEFAULTS(), ...Object.fromEntries(PERSONAL.filter((k) => k in saved).map((k) => [k, saved[k]])) };
  } catch { return DEFAULTS(); }
}

function emit(p: Person, type: string, data: unknown) {
  const id = ++p.seq;
  const line = `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
  p.backlog.push({ id, line });
  if (p.backlog.length > 2000) p.backlog.splice(0, p.backlog.length - 2000); // a reconnecting tab replays what it missed
  for (const r of p.listeners) r.write(line);
}

let observed = false;
async function personFor(req: http.IncomingMessage): Promise<Person | null> {
  if (!CFG.aud) {
    // Not configured yet: refuse everyone. Log the first token's issuer and audience (no email, no token) once, so
    // the operator can set ACCESS_AUD for the Access application the public-entry reconciler created.
    const raw = req.headers['cf-access-jwt-assertion'] as string | undefined;
    if (raw && !observed) {
      try { const c = JSON.parse(Buffer.from(raw.split('.')[1], 'base64url').toString('utf8')); console.log(`access token seen: iss=${c.iss} aud=${JSON.stringify(c.aud)}`); observed = true; } catch { /* not a token */ }
    }
    return null;
  }
  const ident = await verifier.verify(req.headers['cf-access-jwt-assertion'] as string | undefined).catch(() => null);
  if (!ident) return null;
  const id = createHash('sha256').update(ident.email).digest('hex').slice(0, 24);
  let p = people.get(id);
  if (!p) {
    const dir = join(CFG.data, 'people', id);
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    const person = { id, email: ident.email, token: ident.token, exp: ident.exp, dir, settings: loadSettings(dir),
      listeners: new Set(), seq: 0, backlog: [] } as unknown as Person;
    // The MCP sees this person's own Access token (refreshed by each browser request), not a server identity.
    person.link = new SovaLink(() => person.settings, async () => {
      if (person.exp * 1000 <= Date.now()) throw new Error('access_expired');
      return `cfaccess.${person.token}`;
    });
    person.agents = new Agents(person.link, (e: AgentEvent) => emit(person, 'agent', e), () => 'apikey', {
      workspace: () => join(dir, 'workspace'), runtime: () => ({ bin: CFG.codexBin, path: CFG.codexPath }),
      apiKey: readKey, codexHome: join(dir, 'codex'), personalServers: () => [],
    });
    person.link.confirm = (name, input) => person.agents.ask(name, input);
    person.link.reset();
    await person.link.start();
    people.set(id, person);
    p = person;
  }
  if (ident.exp >= p.exp) { p.token = ident.token; p.exp = ident.exp; }
  return p;
}

/* ---------- platform Argo CD (the public read-only UI's API) ---------- */

async function platformArgo() {
  const label = '플랫폼 Argo CD', ui = CFG.argo;
  try {
    const r = await fetch(`${CFG.argo}/api/v1/applications`, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(10_000) });
    if (!r.ok) return { ok: false, side: 'platform', label, ui, code: `http_${r.status}`, apps: [] };
    const list = (await r.json()) as { items?: any[] };
    return { ok: true, side: 'platform', label, ui, uid: 'in-cluster', ns: 'bravo-gitops', apps: mapApps(list.items ?? []) };
  } catch { return { ok: false, side: 'platform', label, ui, code: 'argo_unreachable', apps: [] }; }
}

/* ---------- HTTP ---------- */

const MIME: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff', '.json': 'application/json', '.ico': 'image/x-icon' };
const SECURITY = {
  'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer', 'x-frame-options': 'DENY',
  'permissions-policy': 'camera=(), microphone=(), geolocation=()',
};

function json(res: http.ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { ...SECURITY, 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify(body));
}

async function body(req: http.IncomingMessage): Promise<any> {
  let size = 0; const chunks: Buffer[] = [];
  for await (const c of req) { size += c.length; if (size > 1 << 20) throw new Error('too_large'); chunks.push(c); }
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
}

function serveStatic(req: http.IncomingMessage, res: http.ServerResponse) {
  const path = decodeURIComponent(new URL(req.url ?? '/', 'http://x').pathname);
  let file = normalize(join(CFG.statics, path));
  if (!file.startsWith(CFG.statics + sep) && file !== CFG.statics) return json(res, 404, { detail: { code: 'not_found' } });
  if (!existsSync(file) || statSync(file).isDirectory()) file = join(CFG.statics, 'index.html'); // single-page app
  const asset = file.includes(`${sep}assets${sep}`);
  res.writeHead(200, { ...SECURITY, 'content-type': MIME[extname(file)] ?? 'application/octet-stream',
    'cache-control': asset ? 'public, max-age=31536000, immutable' : 'no-cache' });
  createReadStream(file).pipe(res);
}

function sse(res: http.ServerResponse) {
  res.writeHead(200, { ...SECURITY, 'content-type': 'text/event-stream', 'cache-control': 'no-store', 'x-accel-buffering': 'no' });
  res.write('retry: 2000\n\n');
  const ping = setInterval(() => res.write(': ping\n\n'), 20_000);
  res.on('close', () => clearInterval(ping));
}

const PLAN_EVENTS = /^\/api\/plans\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\/events$/;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://x');
  if (url.pathname === '/healthz') return json(res, 200, { ok: true });
  if (!url.pathname.startsWith('/api/')) return serveStatic(req, res);

  // Writes come only from this site's own pages: same Origin and a header a cross-site form cannot set.
  if (req.method !== 'GET' && (req.headers['x-sova-console'] !== '1' || (req.headers.origin && req.headers.origin !== CFG.origin))) {
    return json(res, 403, { detail: { code: 'cross_site_refused' } });
  }
  const p = await personFor(req);
  if (!p) return json(res, CFG.aud ? 401 : 503, { detail: { code: CFG.aud ? 'access_required' : 'access_not_configured' } });

  try {
    const key = `${req.method} ${url.pathname}`;
    if (key === 'GET /api/stream') {
      sse(res);
      const after = Number(req.headers['last-event-id'] ?? 0);
      for (const b of p.backlog) if (b.id > after) res.write(b.line);
      p.listeners.add(res);
      req.on('close', () => p.listeners.delete(res));
      return;
    }
    const plan = PLAN_EVENTS.exec(url.pathname);
    if (plan && req.method === 'GET') {
      sse(res);
      const stop = new AbortController();
      req.on('close', () => stop.abort());
      // Resume from the browser's last event id; stamp each push with the newest one for its next reconnect.
      let cursor = (req.headers['last-event-id'] as string | undefined) || undefined;
      await followPlan(plan[1], (id, last, signal) => p.link.openEvents(id, last, signal), (push: EventPush) => {
        cursor = push.events?.at(-1)?.event_id ?? cursor;
        res.write(`${cursor ? `id: ${cursor}\n` : ''}data: ${JSON.stringify(push)}\n\n`);
      }, stop.signal, cursor);
      return void res.end();
    }
    const b = req.method === 'POST' ? await body(req) : {};
    switch (key) {
      case 'GET /api/session': return json(res, 200, { email: p.email, exp: p.exp });
      case 'POST /api/call': return json(res, 200, await p.link.call(String(b.name), b.args ?? {}));
      case 'GET /api/status': return json(res, 200, await p.link.status());
      case 'POST /api/resource': return json(res, 200, await p.link.resource(String(b.uri)));
      case 'GET /api/prompts': return json(res, 200, await p.link.prompts());
      case 'POST /api/prompt': return json(res, 200, await p.link.prompt(String(b.name), b.args ?? {}));
      case 'GET /api/settings': return json(res, 200, p.settings);
      case 'POST /api/settings': {
        for (const k of PERSONAL) if (k in b) (p.settings as any)[k] = b[k];
        if (p.settings.planScope && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(p.settings.planScope)) p.settings.planScope = '';
        writeFileSync(join(p.dir, 'settings.json'), JSON.stringify(Object.fromEntries(PERSONAL.map((k) => [k, p.settings[k]]))), { mode: 0o600 });
        p.link.reset(); p.agents.reset();
        return json(res, 200, p.settings);
      }
      case 'GET /api/models': return json(res, 200, { models: codexModels().filter((m) => m.api), defaultModel: 'gpt-6-astra' });
      case 'GET /api/agent/auth': {
        const ok = !!readKey();
        return json(res, 200, { ok, message: ok ? '플랫폼의 OpenAI API 키로 실행합니다.' : '플랫폼에 OpenAI API 키가 아직 없습니다.' });
      }
      case 'POST /api/agent/send': void p.agents.send(String(b.conv), String(b.text), b.opts as TurnOptions); return json(res, 202, {});
      case 'POST /api/agent/stop': await p.agents.stop(String(b.conv)); return json(res, 200, {});
      case 'POST /api/agent/confirm': p.agents.confirm(String(b.id), b.allow === true); return json(res, 200, {});
      case 'GET /api/argo/platform': return json(res, 200, await platformArgo());
      default: return json(res, 404, { detail: { code: 'not_found' } });
    }
  } catch (e) {
    if (!res.headersSent) json(res, (e as Error).message === 'too_large' ? 413 : 400, { detail: { code: (e as Error).message === 'too_large' ? 'too_large' : 'bad_request' } });
    else res.end();
  }
});

server.listen(CFG.port, () => console.log(`sova-console-web listening on ${CFG.port}${CFG.aud ? '' : ' (ACCESS_AUD not set: refusing /api)'}`));
