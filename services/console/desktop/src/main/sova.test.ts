import { describe, expect, it } from 'vitest';
import { confirmRequest } from './sova';

const call = (name: string, args: Record<string, unknown>) =>
  Buffer.from(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }));

describe('confirmRequest', () => {
  it('holds only the confirming call of a state-making tool', () => {
    expect(confirmRequest(call('sova_create_plan', { confirmation: 'tok', workload_id: 'w' }))?.name).toBe('sova_create_plan');
    expect(confirmRequest(call('sova_create_plan', { workload_id: 'w' }))).toBeNull(); // preview
    expect(confirmRequest(call('sova_get_plan', { plan_id: 'p', confirmation: 'x' }))).toBeNull();
    expect(confirmRequest(Buffer.from('not json'))).toBeNull();
  });
});

describe('SovaLink proxy', () => {
  it('refuses the standalone GET stream and stops the upstream request when the caller leaves', async () => {
    const http = await import('node:http');
    const { SovaLink } = await import('./sova');
    let upstreamClosed!: () => void;
    const closed = new Promise<void>((r) => { upstreamClosed = r; });
    const upstream = http.createServer((req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' }); res.write(': open\n\n');
      req.on('close', () => upstreamClosed()); // never ends on its own
    });
    await new Promise<void>((r) => upstream.listen(0, '127.0.0.1', r));
    const port = (upstream.address() as { port: number }).port;
    const link = new SovaLink(() => ({ mcpUrl: `http://127.0.0.1:${port}/mcp`, devToken: 'dev', readOnly: true, planScope: '' }) as any);
    link.reset();
    await link.start();
    const auth = { authorization: `Bearer ${link.secret}` };

    const get = await fetch(link.url, { headers: auth });
    expect(get.status).toBe(405);

    const ctl = new AbortController();
    const post = await fetch(link.url, { method: 'POST', headers: { ...auth, 'content-type': 'application/json' }, body: '{}', signal: ctl.signal });
    expect(post.status).toBe(200);
    ctl.abort();
    await expect(Promise.race([closed.then(() => 'closed'), new Promise((r) => setTimeout(() => r('open'), 3000))])).resolves.toBe('closed');
    upstream.close();
  });
});
