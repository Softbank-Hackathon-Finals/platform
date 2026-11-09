// Progress timeline, laid out like Datadog's Event Explorer (search + facets, count histogram,
// time range, list with a detail side panel) plus a stage swimlane that draws how long each
// migration stage lasted. All marks come from the plan's real event stream (cursor-followed).
import { useAtomValue } from 'jotai';
import { Search, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { StatusBadge, stageLabel } from './brand';
import { useT } from './i18n';
import { ago, planAtom } from './lib';
import { useEvents } from './views';

const TYPES = [
  ['state.changed', '단계 변경', '#1E88D9'], ['gate.completed', '원본 대조', '#7A6450'], ['approval.requested', '승인 요청', '#FFD23F'],
  ['approval.decided', '승인 결정', '#8CC152'], ['execution.updated', '실행', '#D42A1E'], ['failback.recorded', '복귀', '#5A231A'],
] as const;
const COLOR = Object.fromEntries(TYPES.map(([k, , c]) => [k, c]));
const LABEL = Object.fromEntries(TYPES.map(([k, l]) => [k, l]));
const STAGE_COLOR: Record<string, string> = {
  COLLECT: '#E9DCC0', ANALYZE: '#D6C3A6', PROPOSE: '#C9AE8A', REHEARSE: '#B9966E', GATE: '#7A6450', AWAIT_APPROVAL: '#FFD23F',
  SHIFT: '#1E88D9', CUTOVER: '#D42A1E', WATCH: '#8CC152', DONE: '#4C7A1C', FAILBACK: '#5A231A', SAFE_HOLD: '#FF9E3D', NEEDS_HUMAN: '#FF9E3D',
};
const RANGES = [['all', '전체'], ['1h', '1시간'], ['6h', '6시간'], ['24h', '24시간']] as const;
const RANGE_MS: Record<string, number> = { '1h': 3.6e6, '6h': 2.16e7, '24h': 8.64e7 };

function summary(e: any) {
  const d = e.data ?? {};
  switch (e.event_type) {
    case 'state.changed': return `${stageLabel(d.from)} → ${stageLabel(d.to)}`;
    case 'gate.completed': return `${d.gate_id} · ${d.decision}`;
    case 'approval.requested': return `${d.approval_kind} 승인 요청`;
    case 'approval.decided': return `${d.approval_kind ?? ''} ${d.decision ?? ''}`.trim();
    case 'execution.updated': return `${d.step} · ${d.status}${d.elapsed_ms ? ` · ${(d.elapsed_ms / 1000).toFixed(1)}s` : ''}`;
    case 'failback.recorded': return `${d.status ?? ''} ${d.command_id ?? ''}`.trim();
    default: return '';
  }
}
const tone = (e: any) => e.event_type === 'gate.completed' ? (e.data?.decision === 'PASS' ? 'PASSED' : 'FAILED')
  : e.event_type === 'execution.updated' ? ({ succeeded: 'PASSED', failed: 'FAILED', started: 'RUNNING', unknown: 'UNKNOWN' } as any)[e.data?.status] ?? 'OK'
    : e.event_type === 'approval.requested' ? 'BLOCKED' : 'OK';
const fmt = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

export function Timeline() {
  const t = useT();
  const plan = useAtomValue(planAtom);
  const { events, error } = useEvents(plan);
  const [q, setQ] = useState('');
  const [off, setOff] = useState<Set<string>>(new Set());
  const [range, setRange] = useState<string>('all');
  const [sel, setSel] = useState<any | null>(null);

  const all = useMemo(() => events.map((e) => ({ ...e, ts: new Date(e.occurred_at).getTime() })), [events]);
  const now = Date.now();
  const inRange = all.filter((e) => range === 'all' || e.ts >= now - RANGE_MS[range]);
  const shown = inRange.filter((e) => !off.has(e.event_type) && (!q || `${e.event_type} ${summary(e)} ${JSON.stringify(e.data)}`.toLowerCase().includes(q.toLowerCase())));
  const counts = Object.fromEntries(TYPES.map(([k]) => [k, inRange.filter((e) => e.event_type === k).length]));
  const t0 = inRange.length ? Math.min(...inRange.map((e) => e.ts)) : now - 3.6e6;
  const t1 = Math.max(now, ...inRange.map((e) => e.ts));
  const x = (ts: number) => ((ts - t0) / Math.max(1, t1 - t0)) * 100;

  // Histogram: 40 buckets, stacked by type.
  const buckets = Array.from({ length: 40 }, () => ({} as Record<string, number>));
  for (const e of shown) { const b = Math.min(39, Math.floor(((e.ts - t0) / Math.max(1, t1 - t0)) * 40)); buckets[b][e.event_type] = (buckets[b][e.event_type] ?? 0) + 1; }
  const peak = Math.max(1, ...buckets.map((b) => Object.values(b).reduce((a, n) => a + n, 0)));

  // Stage spans from state.changed (the last stage runs until now).
  // Ordered by the ledger sequence (monotonic per plan), not by wall clock.
  const changes = all.filter((e) => e.event_type === 'state.changed').sort((a, b) => a.sequence - b.sequence);
  const spans = changes.map((e, i) => ({ stage: e.data?.to as string, from: e.ts, to: changes[i + 1]?.ts ?? now }))
    .filter((s) => s.to >= t0);
  const ticks = Array.from({ length: 6 }, (_, i) => t0 + ((t1 - t0) * i) / 5);
  const toggle = (k: string) => setOff((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  return (
    <div className="relative flex h-full min-w-0">
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Search + time range */}
        <div className="flex items-center gap-2 border-b-2 border-ink bg-cream px-4 py-2">
          <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border-2 border-ink bg-[#fffaf0] px-2.5 shadow-pop-sm">
            <Search size={14} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="이벤트 검색 — 예: cutover, PASS, gate" className="min-w-0 flex-1 bg-transparent py-1.5 text-[12.5px] outline-none" />
            {q && <button aria-label="검색 지우기" onClick={() => setQ('')}><X size={14} /></button>}
          </div>
          <div className="flex overflow-hidden rounded-lg border-2 border-ink">
            {RANGES.map(([id, l]) => (
              <button key={id} onClick={() => setRange(id)} className={`border-r-2 border-ink px-2.5 py-1 text-[12px] font-bold last:border-r-0 ${range === id ? 'bg-mustard' : 'bg-[#fffaf0] hover:bg-paper'}`}>{l}</button>
            ))}
          </div>
          <span className="whitespace-nowrap font-mono text-[11px] text-ink-soft">{shown.length}/{all.length}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 border-b-2 border-ink bg-paper px-4 py-1.5">
          <span className="kicker mr-1">이벤트 유형</span>
          {TYPES.map(([k, l, c]) => (
            <button key={k} onClick={() => toggle(k)}
              className={`flex items-center gap-1.5 rounded-full border-2 px-2 py-0.5 text-[11.5px] font-bold ${off.has(k) ? 'border-ink/20 text-ink-soft line-through' : 'border-ink bg-[#fffaf0] shadow-pop-sm'}`}>
              <span className="h-2.5 w-2.5 rounded-full border border-ink" style={{ background: c }} />{t(l)}<span className="font-mono text-[10.5px] text-ink-soft">{counts[k]}</span>
            </button>
          ))}
        </div>

        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
          {error ? <p className="m-4 rounded-lg border-2 border-dashed border-ink p-3 text-[12.5px]">이벤트를 읽지 못했습니다. 결과는 미확인입니다.</p> : null}
          {/* Histogram */}
          <section className="border-b-2 border-ink bg-[#fffaf0] px-4 pb-2 pt-3">
            <div className="kicker mb-1">시간대별 이벤트 수</div>
            <div className="flex h-20 items-end gap-[3px]">
              {buckets.map((b, i) => (
                <div key={i} className="flex flex-1 flex-col-reverse overflow-hidden rounded-t-sm" title={`${fmt(t0 + ((t1 - t0) * i) / 40)} · ${Object.values(b).reduce((a, n) => a + n, 0)}건`}>
                  {TYPES.filter(([k]) => b[k]).map(([k, , c]) => <div key={k} style={{ height: `${(b[k] / peak) * 76}px`, background: c }} className="border-x border-t border-ink/60" />)}
                </div>
              ))}
            </div>
            <div className="mt-1 flex justify-between font-mono text-[10px] text-ink-soft">{ticks.map((tk) => <span key={tk}>{fmt(tk)}</span>)}</div>
          </section>

          {/* Stage swimlane + event markers */}
          <section className="border-b-2 border-ink px-4 py-3">
            <div className="kicker mb-2">단계 구간 · 소요 시간</div>
            <div className="relative h-9 rounded-lg border-2 border-ink bg-cream">
              {spans.map((s, i) => (
                <div key={i} className="absolute inset-y-0 flex items-center overflow-hidden border-r-2 border-ink px-1.5 text-[10.5px] font-bold"
                  style={{ left: `${Math.max(0, x(s.from))}%`, width: `${Math.max(0.6, x(s.to) - Math.max(0, x(s.from)))}%`, background: STAGE_COLOR[s.stage] ?? '#ddd', color: ['SHIFT', 'CUTOVER', 'FAILBACK', 'GATE', 'DONE'].includes(s.stage) ? '#fff3dc' : '#1b1b1b' }}
                  title={`${stageLabel(s.stage)} · ${Math.round((s.to - s.from) / 60000)}분`}>
                  <span className="truncate">{t(stageLabel(s.stage))} · {Math.max(1, Math.round((s.to - s.from) / 60000))}m</span>
                </div>
              ))}
            </div>
            {TYPES.filter(([k]) => k !== 'state.changed' && !off.has(k)).map(([k, l, c]) => (
              <div key={k} className="mt-1.5 flex items-center gap-2">
                <span className="w-[72px] shrink-0 text-right text-[11px] text-ink-soft">{t(l)}</span>
                <div className="relative h-5 flex-1 rounded border border-dashed border-ink/30">
                  {shown.filter((e) => e.event_type === k).map((e) => (
                    <button key={e.event_id} onClick={() => setSel(e)} title={summary(e)}
                      className={`absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rotate-45 border-2 border-ink ${sel?.event_id === e.event_id ? 'scale-125' : ''}`}
                      style={{ left: `${x(e.ts)}%`, background: c }} />
                  ))}
                </div>
              </div>
            ))}
          </section>

          {/* List */}
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="sticky top-0 bg-paper">
              <tr>{['시각', '유형', '내용', '상태', '#'].map((h) => <th key={h} className="kicker border-b-2 border-ink px-3 py-1.5 text-left !text-[9.5px]">{h}</th>)}</tr>
            </thead>
            <tbody>
              {[...shown].reverse().map((e) => (
                <tr key={e.event_id} onClick={() => setSel(e)} className={`cursor-default border-b border-dashed border-ink/20 hover:bg-paper ${sel?.event_id === e.event_id ? 'bg-mustard/40' : ''}`}>
                  <td className="whitespace-nowrap px-3 py-1.5 font-mono text-[11px]">{new Date(e.ts).toLocaleString()}<span className="ml-1.5 text-ink-soft">{ago(e.occurred_at)}</span></td>
                  <td className="px-3 py-1.5"><span className="inline-flex items-center gap-1.5 whitespace-nowrap"><span className="h-2.5 w-2.5 rounded-full border border-ink" style={{ background: COLOR[e.event_type] }} />{t(LABEL[e.event_type] ?? e.event_type)}</span></td>
                  <td className="px-3 py-1.5">{summary(e)}</td>
                  <td className="px-3 py-1.5"><StatusBadge status={tone(e)} /></td>
                  <td className="px-3 py-1.5 font-mono text-[11px] text-ink-soft">{e.sequence}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!shown.length && !error && <p className="p-6 text-center text-[12.5px] text-ink-soft">조건에 맞는 이벤트가 없습니다.</p>}
        </div>
      </div>

      {/* Detail side panel */}
      {sel && (
        <aside className="pop-in absolute bottom-3 right-3 top-3 z-20 w-[340px] overflow-y-auto rounded-2xl border-2 border-ink bg-[#fffaf0] shadow-pop-lg">
          <div className="flex items-center gap-2 border-b-2 border-ink px-4 py-3" style={{ background: COLOR[sel.event_type] }}>
            <span className="font-kdisplay text-[15px] text-ink [text-shadow:0_0_6px_#fff3dc]">{t(LABEL[sel.event_type] ?? sel.event_type)}</span>
            <div className="flex-1" />
            <button aria-label="닫기" className="rounded bg-cream p-0.5" onClick={() => setSel(null)}><X size={14} /></button>
          </div>
          <dl className="grid grid-cols-[92px_1fr] gap-x-3 gap-y-1.5 px-4 py-3 text-[12px]">
            <dt className="text-ink-soft">내용</dt><dd className="font-bold">{summary(sel)}</dd>
            <dt className="text-ink-soft">시각</dt><dd className="font-mono">{new Date(sel.ts).toLocaleString()}</dd>
            <dt className="text-ink-soft">순번</dt><dd className="font-mono">#{sel.sequence}</dd>
            <dt className="text-ink-soft">실행 ID</dt><dd data-selectable className="truncate font-mono">{sel.run_id}</dd>
            <dt className="text-ink-soft">이벤트 ID</dt><dd data-selectable className="truncate font-mono">{sel.event_id}</dd>
          </dl>
          <pre data-selectable className="scroll-thin mx-4 max-h-[50vh] overflow-auto rounded-lg border-2 border-ink bg-ink p-3 font-mono text-[11px] text-cream">{JSON.stringify(sel.data, null, 2)}</pre>
          <p className="px-4 py-2 text-[11px] text-ink-soft">T10 이벤트 원장의 원문입니다. 판정·실행은 서버가 했고 이 화면은 읽기만 합니다.</p>
        </aside>
      )}
    </div>
  );
}
