// Live plan progress: the T10 SSE event contract served by the SOVA MCP host
// (GET /v1/plans/{id}/events). The server ends the stream when the ≤60 s proof expires,
// so each reconnect carries a new proof and Last-Event-ID; nothing is replayed or skipped.
import type { EventPush } from '../shared/types';

export interface SseEvent { id?: string; event?: string; data: string }

/** Splits complete SSE blocks off the buffer; comments and retry lines are dropped. */
export function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const blocks = buffer.replace(/\r\n?/g, '\n').split('\n\n');
  const rest = blocks.pop() ?? '';
  const events: SseEvent[] = [];
  for (const block of blocks) {
    const e: SseEvent = { data: '' };
    const data: string[] = [];
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'id') e.id = value;
      else if (field === 'event') e.event = value;
      else if (field === 'data') data.push(value);
    }
    if (data.length) events.push({ ...e, data: data.join('\n') });
  }
  return { events, rest };
}

type Open = (planId: string, lastEventId: string | undefined, signal: AbortSignal) => Promise<Response>;

/** One plan's stream until stopped. Reports 'live', 'retrying' (with the code), or 'unsupported' (old host: poll instead). */
export async function followPlan(planId: string, open: Open, push: (p: EventPush) => void, signal: AbortSignal) {
  let last: string | undefined;
  let failures = 0;
  while (!signal.aborted) {
    let code = 'stream_closed';
    try {
      const res = await open(planId, last, signal);
      if (res.status === 404) { push({ mode: 'unsupported' }); return; }
      if (res.status === 409) { last = undefined; push({ mode: 'retrying', reset: true, code: 'resync_required' }); continue; }
      if (!res.ok || !res.headers.get('content-type')?.startsWith('text/event-stream') || !res.body) {
        code = await res.json().then((j) => j?.detail?.code ?? `upstream_${res.status}`, () => `upstream_${res.status}`);
        throw new Error(code);
      }
      push({ mode: 'live' });
      failures = 0;
      const decoder = new TextDecoder();
      let buffer = '';
      for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
        const parsed = parseSse(buffer + decoder.decode(chunk, { stream: true }));
        buffer = parsed.rest;
        const events = parsed.events.flatMap((e) => { try { return [JSON.parse(e.data)]; } catch { return []; } });
        if (events.length) { last = parsed.events.at(-1)!.id ?? last; push({ mode: 'live', events }); }
      }
      // Proof expired or host restarted: reconnect from the cursor with a new proof.
      await new Promise((r) => setTimeout(r, 250));
      continue;
    } catch (e) {
      if (signal.aborted) return;
      failures += 1;
      push({ mode: 'retrying', code: e instanceof Error && e.message !== 'fetch failed' ? e.message : 'mcp_unreachable' });
    }
    await new Promise((r) => setTimeout(r, Math.min(15_000, 500 * 2 ** failures)));
  }
}
