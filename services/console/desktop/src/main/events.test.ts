import { describe, expect, it, vi } from 'vitest';
import type { EventPush } from '../shared/types';
import { followPlan, parseSse } from './events';

const sse = (...chunks: string[]) => new Response(new ReadableStream({
  start(c) { for (const x of chunks) c.enqueue(new TextEncoder().encode(x)); c.close(); },
}), { headers: { 'content-type': 'text/event-stream; charset=utf-8' } });
const json = (status: number, code: string) => new Response(JSON.stringify({ detail: { code } }), { status });
const event = (n: number) => `id: p:${n}\nevent: plan.stage_changed\ndata: {"event_id":"p:${n}"}\n\n`;

describe('parseSse', () => {
  it('keeps an unfinished block for the next chunk and drops comments and retry', () => {
    const a = parseSse('retry: 1000\n\n: keep-alive\n\nid: p:1\nevent: x\ndata: {"a":\ndata: 1}\n\nid: p:2\nda');
    expect(a.events).toEqual([{ id: 'p:1', event: 'x', data: '{"a":\n1}' }]);
    const b = parseSse(a.rest + 'ta: {}\r\n\r\n');
    expect(b.events).toEqual([{ id: 'p:2', data: '{}' }]);
    expect(b.rest).toBe('');
  });
});

describe('followPlan', () => {
  it('resumes from the last event id after the host ends the stream, and stops on an old host', async () => {
    const opened: (string | undefined)[] = [];
    const pushes: EventPush[] = [];
    const replies = [sse(event(1), 'id: p:2\nevent: x\nda', 'ta: {"event_id":"p:2"}\n\n'), sse(': keep-alive\n\n', event(3)), json(404, 'not_found')];
    await followPlan('p', async (_p, last) => { opened.push(last); return replies.shift()!; }, (p) => pushes.push(p), new AbortController().signal);
    expect(opened).toEqual([undefined, 'p:2', 'p:3']);
    expect(pushes.flatMap((p) => p.events ?? []).map((e) => e.event_id)).toEqual(['p:1', 'p:2', 'p:3']);
    expect(pushes.at(-1)).toEqual({ mode: 'unsupported' });
  });

  it('reports a refusal by its code and starts over on resync_required', async () => {
    const ctl = new AbortController();
    const pushes: EventPush[] = [];
    const replies = [json(403, 'forbidden'), json(409, 'resync_required'), sse(event(1))];
    await followPlan('p', async () => { const r = replies.shift(); if (!r) { ctl.abort(); throw new Error('aborted'); } return r; },
      (p) => pushes.push(p), ctl.signal);
    expect(pushes.slice(0, 2)).toEqual([{ mode: 'retrying', code: 'forbidden' }, { mode: 'retrying', reset: true, code: 'resync_required' }]);
    expect(pushes.some((p) => p.events?.[0]?.event_id === 'p:1')).toBe(true);
  });
});

describe('followPlan timeout', () => {
  it('retries a request that never returns headers', async () => {
    const ctl = new AbortController();
    const pushes: EventPush[] = [];
    let calls = 0;
    const open = (_p: string, _l: string | undefined, signal: AbortSignal) => {
      calls += 1;
      if (calls === 1) return new Promise<Response>((_, reject) => signal.addEventListener('abort', () => reject(signal.reason)));
      ctl.abort(); return Promise.reject(new Error('aborted'));
    };
    vi.useFakeTimers();
    const run = followPlan('p', open, (p) => pushes.push(p), ctl.signal);
    await vi.advanceTimersByTimeAsync(15_100);
    await vi.advanceTimersByTimeAsync(2_000);
    await run;
    vi.useRealTimers();
    expect(pushes[0]).toEqual({ mode: 'retrying', code: 'stream_timeout' });
    expect(calls).toBe(2);
  });
});
