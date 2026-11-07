// Real-time observability, after REA of Grafana (refresh picker incl. Live streaming, time range,
// shared crosshair, annotations, panel menu view/inspect, streaming chrome) and Netdata (Play /
// Pause / Force Play, highlight window vs. baseline). Values are the platform observability API's
// current 1-minute windows read through SOVA MCP; the series is what this console collected since
// it opened (the API serves no history yet), and is labeled so.
import { useAtom, useAtomValue } from 'jotai';
import { atomWithStorage } from 'jotai/utils';
import { atom } from 'jotai';
import { Maximize2, Pause, Play, Radio, ScanSearch, X, Zap } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { stageLabel } from './brand';
import { useT } from './i18n';
import { callTool, planAtom, SovaError, useTool } from './lib';
import { useConsoleLinks } from './settings';
import { useEvents } from './views';

/* ---------- state ---------- */

export type PlayState = 'playing' | 'paused' | 'force';
export const liveAtom = atomWithStorage<{ play: PlayState; every: number; range: number }>('sova.live', { play: 'playing', every: 5, range: 15 });
export const crosshairAtom = atom<number | null>(null);
export const highlightAtom = atom<{ from: number; to: number } | null>(null);
export const REFRESH = [[0, '끄기'], [2, '라이브'], [5, '5초'], [10, '10초'], [30, '30초'], [60, '1분']] as const;
export const RANGES = [[5, '5분'], [15, '15분'], [60, '1시간']] as const;

export interface Sample {
  t: number; rps: number | null; err: number | null; p95: number | null;
  lag: number | null; active: number | null; waiting: number | null; dbUp: boolean | null; poolUp: boolean | null;
}
interface EnvSeries { env: string; name: string; kind: string; samples: Sample[]; lastAt?: string; lastSecondsAgo?: number; error?: string }

// One store for all panels; panels subscribe instead of re-polling.
const store = { envs: new Map<string, EnvSeries>(), alerts: [] as any[], at: 0, listeners: new Set<() => void>() };
const emit = () => { store.at = Date.now(); store.listeners.forEach((l) => l()); };
const subscribe = (l: () => void) => { store.listeners.add(l); return () => store.listeners.delete(l); };
export function useLiveStore() { useSyncExternalStore(subscribe, () => store.at); return store; }

const MAX = 60 * 60 / 2; // one hour at the fastest (Live = 2 s) cadence

async function poll(envs: any[]) {
  await Promise.all(envs.map(async (e) => {
    const cur = store.envs.get(e.environment_id) ?? { env: e.environment_id, name: e.name ?? e.environment_id, kind: e.kind, samples: [] };
    try {
      const [h, d, p] = await Promise.all(['http', 'database', 'pgbouncer'].map((view) =>
        callTool('sova_get_observability', { environment_id: e.environment_id, view }).then((r) => r.data)));
      const slots: any[] = d.replication_slots ?? [];
      const dbs = (p.poolers ?? []).flatMap((x: any) => x.databases ?? []);
      const s: Sample = {
        t: Date.parse(h.queried_at) || Date.now(), rps: h.requests_per_second ?? null, err: h.error_ratio ?? null, p95: h.p95_seconds ?? null,
        lag: slots.length ? Math.max(...slots.map((x) => x.lag_bytes ?? 0)) : null,
        active: dbs.length ? dbs.reduce((a: number, x: any) => a + (x.clients_active ?? 0), 0) : null,
        waiting: dbs.length ? dbs.reduce((a: number, x: any) => a + (x.clients_waiting ?? 0), 0) : null,
        dbUp: (d.instances ?? []).length ? (d.instances as any[]).every((x) => x.up) : null,
        poolUp: (p.poolers ?? []).length ? (p.poolers as any[]).every((x) => x.up) : null,
      };
      const last = cur.samples.at(-1);
      const samples = last && last.t === s.t ? cur.samples : [...cur.samples, s].slice(-MAX);
      store.envs.set(e.environment_id, { ...cur, samples, lastAt: h.queried_at, lastSecondsAgo: h.last_received_seconds_ago, error: undefined, db: d, pool: p } as any);
    } catch (err) {
      store.envs.set(e.environment_id, { ...cur, error: err instanceof SovaError ? err.code : 'mcp_unreachable' });
    }
  }));
  const connected = envs.find((e) => !store.envs.get(e.environment_id)?.error); // alerts are global; any connected env authorizes
  if (connected) try { store.alerts = (await callTool('sova_get_observability', { environment_id: connected.environment_id, view: 'alerts' })).data.alerts ?? []; } catch { /* keep last */ }
  emit();
}

/** The single poller (mount once). Netdata semantics: pauses when the window is hidden unless Force Play. */
export function useLivePoller() {
  const live = useAtomValue(liveAtom);
  const ctx = useTool('sova_get_context', {}, 60_000);
  const envs: any[] = useMemo(() => ctx.data?.data?.environments ?? [], [ctx.data]);
  useEffect(() => {
    if (!envs.length || live.every === 0 || live.play === 'paused') return;
    let stop = false, first = true;
    const tick = async () => {
      if (stop) return;
      if (first || live.play === 'force' || document.visibilityState === 'visible') await poll(envs);
      first = false;
      if (!stop) timer = setTimeout(tick, live.every * 1000);
    };
    let timer = setTimeout(tick, 0);
    return () => { stop = true; clearTimeout(timer); };
  }, [envs, live.every, live.play]);
}

/* ---------- toolbar ---------- */

export function LiveToolbar() {
  const t = useT();
  const [live, setLive] = useAtom(liveAtom);
  const [hl, setHl] = useAtom(highlightAtom);
  const s = useLiveStore();
  const age = s.at ? Math.round((Date.now() - s.at) / 1000) : null;
  const stale = age !== null && live.every > 0 && age > live.every * 3;
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest('input,textarea,select')) return;
      if (e.key === ' ') { e.preventDefault(); setLive((l) => ({ ...l, play: l.play === 'paused' ? 'playing' : 'paused' })); }
      if (e.key === 'Escape') setHl(null);
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [setLive, setHl]);
  const PlayIcon = live.play === 'paused' ? Pause : live.play === 'force' ? Zap : Play;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex overflow-hidden rounded-lg border-2 border-ink shadow-pop-sm">
        {(['playing', 'paused', 'force'] as const).map((p) => (
          <button key={p} onClick={() => setLive({ ...live, play: p })} title={p === 'force' ? '창이 가려져도 계속 갱신 (Force Play)' : p === 'playing' ? '보이는 동안 갱신 (Play) · Space' : '멈춤 (Pause) · Space'}
            className={`flex items-center gap-1 border-r-2 border-ink px-2 py-1 text-[11.5px] font-bold last:border-r-0 ${live.play === p ? (p === 'paused' ? 'bg-ink text-cream' : 'bg-wasabi') : 'bg-[#fffaf0] hover:bg-paper'}`}>
            {p === 'playing' ? <Play size={12} /> : p === 'paused' ? <Pause size={12} /> : <Zap size={12} />}{t(p === 'playing' ? '재생' : p === 'paused' ? '일시정지' : '강제 재생')}
          </button>
        ))}
      </div>
      <div className="flex items-center overflow-hidden rounded-lg border-2 border-ink shadow-pop-sm" title="자동 새로 고침 간격 (Grafana refresh picker)">
        {REFRESH.map(([v, l]) => (
          <button key={v} onClick={() => setLive({ ...live, every: v })}
            className={`flex items-center gap-1 border-r-2 border-ink px-2 py-1 text-[11.5px] font-bold last:border-r-0 ${live.every === v ? 'bg-mustard' : 'bg-[#fffaf0] hover:bg-paper'}`}>
            {v === 2 && <Radio size={12} />}{t(l)}
          </button>
        ))}
      </div>
      <div className="flex overflow-hidden rounded-lg border-2 border-ink shadow-pop-sm" title="시간 범위">
        {RANGES.map(([v, l]) => (
          <button key={v} onClick={() => setLive({ ...live, range: v })} className={`border-r-2 border-ink px-2 py-1 text-[11.5px] font-bold last:border-r-0 ${live.range === v ? 'bg-mustard' : 'bg-[#fffaf0] hover:bg-paper'}`}>{t('최근')} {t(l)}</button>
        ))}
      </div>
      {hl && <button className="btn !py-0.5 text-[11px]" onClick={() => setHl(null)}><X size={12} /> {t('하이라이트 해제')} (Esc)</button>}
      <span className={`ml-auto flex items-center gap-1.5 whitespace-nowrap rounded-full border-2 px-2 py-0.5 text-[11px] font-bold ${stale ? 'border-red bg-[#ffe3d6]' : 'border-ink/20'}`}>
        <PlayIcon size={11} />{live.play === 'paused' || live.every === 0 ? t('멈춤') : stale ? `${t('지연')} · ${age}${t('초 전')}` : age === null ? t('수집 대기') : `${t('수신')} ${age}${t('초 전')}`}
      </span>
    </div>
  );
}

/* ---------- chart ---------- */

export interface Line { key: string; label: string; color: string; points: [number, number | null][] }
const fmtTime = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

export function LiveChart({ title, unit, lines, format, threshold, height = 150, annotations = [] }: {
  title: string; unit?: string; lines: Line[]; format: (v: number) => string; threshold?: { value: number; label: string };
  height?: number; annotations?: { t: number; label: string }[];
}) {
  const t = useT();
  const live = useAtomValue(liveAtom);
  const [cross, setCross] = useAtom(crosshairAtom);
  const [hl, setHl] = useAtom(highlightAtom);
  const [menu, setMenu] = useState(false);
  const [mode, setMode] = useState<'chart' | 'data' | 'full'>('chart');
  const [drag, setDrag] = useState<number | null>(null);
  const box = useRef<SVGSVGElement>(null);
  // The SVG stretches (preserveAspectRatio none), so every glyph and dot is HTML on top of it, never SVG text.
  const W = 1000, H = height - 18;
  const t1 = Date.now(), t0 = t1 - live.range * 60_000;
  const all = lines.flatMap((l) => l.points.filter(([t, v]) => t >= t0 && v !== null).map(([, v]) => v as number));
  const top = Math.max(threshold?.value ?? 0, ...(all.length ? all : [1])) || 1;
  const max = top * 1.15;
  const x = (t: number) => ((t - t0) / (t1 - t0)) * W;
  const y = (v: number) => H - (v / max) * (H - 6);
  const pct = (px: number, total: number) => `${(px / total) * 100}%`;
  const toT = (clientX: number) => { const r = box.current!.getBoundingClientRect(); return t0 + ((clientX - r.left) / r.width) * (t1 - t0); };
  const at = (pts: [number, number | null][], t: number) => pts.reduce<[number, number | null] | null>((best, p) => (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t) ? p : best), null);
  const stats = (pts: [number, number | null][], inside: boolean) => {
    const v = pts.filter(([t, val]) => val !== null && t >= t0 && (hl ? (t >= hl.from && t <= hl.to) === inside : true)).map(([, val]) => val as number);
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  };
  const streaming = live.play !== 'paused' && live.every > 0;
  const visible = lines.map((l) => ({ ...l, pts: l.points.filter(([t, v]) => t >= t0 && v !== null) as [number, number][] }));
  const chart = (
    <div className="flex h-full flex-col">
      <div className="relative min-h-0 flex-1">
        <svg ref={box} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full cursor-crosshair select-none"
          onMouseMove={(e) => setCross(toT(e.clientX))} onMouseLeave={() => { setCross(null); setDrag(null); }}
          onMouseDown={(e) => setDrag(toT(e.clientX))}
          onMouseUp={(e) => { if (drag !== null) { const tt = toT(e.clientX); if (Math.abs(tt - drag) > (t1 - t0) / 100) setHl({ from: Math.min(drag, tt), to: Math.max(drag, tt) }); } setDrag(null); }}>
          {[0.25, 0.5, 0.75, 1].map((f) => <line key={f} x1={0} x2={W} y1={y(top * f)} y2={y(top * f)} stroke="#1B1B1B" strokeOpacity=".09" vectorEffect="non-scaling-stroke" />)}
          {hl && <rect x={x(hl.from)} width={Math.max(2, x(hl.to) - x(hl.from))} y={0} height={H} fill="#FFD23F" fillOpacity=".35" />}
          {drag !== null && cross !== null && <rect x={x(Math.min(drag, cross))} width={Math.abs(x(cross) - x(drag))} y={0} height={H} fill="#FFD23F" fillOpacity=".25" />}
          {threshold && <line x1={0} x2={W} y1={y(threshold.value)} y2={y(threshold.value)} stroke="#D42A1E" strokeDasharray="6 5" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />}
          {annotations.filter((a) => a.t >= t0).map((a, i) => <line key={i} x1={x(a.t)} x2={x(a.t)} y1={0} y2={H} stroke="#7A6450" strokeDasharray="3 3" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />)}
          {visible.map((l) => {
            if (!l.pts.length) return null;
            const d = l.pts.map(([t, v], i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
            return <g key={l.key}>
              <path d={`${d} L${x(l.pts.at(-1)![0])} ${H} L${x(l.pts[0][0])} ${H} Z`} fill={l.color} fillOpacity=".1" />
              <path d={d} stroke={l.color} strokeWidth="2.4" fill="none" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
            </g>;
          })}
          {cross !== null && cross >= t0 && <line x1={x(cross)} x2={x(cross)} y1={0} y2={H} stroke="#1B1B1B" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />}
        </svg>
        <span className="tnum pointer-events-none absolute left-1 top-0 rounded bg-[#fffaf0]/85 px-1 text-[10px] text-ink-soft">{format(top)}</span>
        {threshold && <span className="pointer-events-none absolute right-1 -translate-y-full whitespace-nowrap text-[10px] font-bold text-red" style={{ top: pct(y(threshold.value), H) }}>{threshold.label}</span>}
        {annotations.filter((a) => a.t >= t0).map((a, i) => (
          <span key={i} title={`${fmtTime(a.t)} · ${a.label}`} className="absolute top-0 h-2 w-2 -translate-x-1/2 rotate-45 border border-ink bg-soba" style={{ left: pct(x(a.t), W) }} />
        ))}
        {visible.map((l) => l.pts.length ? (
          <span key={l.key} className="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-[1.5px] border-ink"
            style={{ left: pct(x(l.pts.at(-1)![0]), W), top: pct(y(l.pts.at(-1)![1]), H), background: l.color }} />
        ) : null)}
      </div>
      <div className="tnum flex h-[18px] items-end justify-between px-0.5 text-[10px] text-ink-soft">
        {[0, 0.5, 1].map((f) => <span key={f} className="whitespace-nowrap">{fmtTime(t0 + (t1 - t0) * f)}</span>)}
      </div>
    </div>
  );
  const body = mode === 'data' ? (
    <pre data-selectable className="scroll-thin h-full overflow-auto bg-ink p-2 font-mono text-[10.5px] text-cream">
      {JSON.stringify(lines.map((l) => ({ series: l.label, points: l.points.filter(([t]) => t >= t0).map(([t, v]) => [new Date(t).toISOString(), v]) })), null, 1)}
    </pre>
  ) : chart;
  const panel = (
    <section className={`panel flex flex-col ${mode === 'full' ? 'fixed inset-8 z-50 !shadow-pop-lg' : ''}`}>
      <header className="flex items-center gap-2 border-b-2 border-ink px-3 py-1.5">
        {streaming && <span className="live-dot h-2 w-2 rounded-full border border-ink bg-wasabi" title="스트리밍" />}
        <h3 className="min-w-0 flex-1 truncate font-kdisplay text-[13px]">{t(title)}{unit ? <span className="ml-1 font-sans text-[11px] text-ink-soft">({unit})</span> : null}</h3>
        {lines.map((l) => {
          const p = cross !== null ? at(l.points, cross) : l.points.at(-1);
          return <span key={l.key} className="flex items-center gap-1 whitespace-nowrap text-[11px]"><span className="h-2 w-3 rounded-sm border border-ink" style={{ background: l.color }} />{t(l.label)} <b className="tnum text-[12px]">{p?.[1] === null || p === undefined || p === null ? t('미관측') : format(p[1] as number)}</b></span>;
        })}
        <div className="relative">
          <button aria-label="패널 메뉴" className="rounded px-1 hover:bg-paper" onClick={() => setMenu(!menu)}>⋯</button>
          {menu && <>
            <div className="fixed inset-0 z-30" onClick={() => setMenu(false)} />
            <div className="panel pop-in absolute right-0 top-6 z-40 w-[150px] p-1 !shadow-pop">
              <button className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[12px] hover:bg-mustard" onClick={() => { setMode(mode === 'full' ? 'chart' : 'full'); setMenu(false); }}><Maximize2 size={12} /> {t(mode === 'full' ? '패널 닫기' : '패널 보기')}</button>
              <button className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[12px] hover:bg-mustard" onClick={() => { setMode(mode === 'data' ? 'chart' : 'data'); setMenu(false); }}><ScanSearch size={12} /> {t(mode === 'data' ? '차트로' : '데이터 검사')}</button>
            </div>
          </>}
        </div>
      </header>
      <div className={mode === 'full' ? 'min-h-0 flex-1 px-1 pt-1' : 'px-1 pt-1'} style={mode === 'full' ? undefined : { height }}>{body}</div>
      {hl && (
        <footer className="flex flex-wrap gap-3 border-t-2 border-dashed border-ink/25 px-3 py-1 text-[11px]">
          <span className="kicker">{t('하이라이트 vs 기준')}</span>
          {lines.map((l) => {
            const a = stats(l.points, true), b = stats(l.points, false);
            const pct = a !== null && b ? ((a - b) / b) * 100 : null;
            return <span key={l.key} className="whitespace-nowrap">{t(l.label)}: <b>{a === null ? '—' : format(a)}</b> vs {b === null ? '—' : format(b)}{pct !== null && <b className={pct > 0 ? 'text-red' : 'text-[#4c7a1c]'}> {pct > 0 ? '▲' : '▼'}{Math.abs(pct).toFixed(1)}%</b>}</span>;
          })}
        </footer>
      )}
    </section>
  );
  return mode === 'full' ? <><div className="fixed inset-0 z-40 bg-ink/40" onClick={() => setMode('chart')} />{panel}</> : panel;
}

/* ---------- panels built on the store ---------- */

const COLORS: Record<string, string> = { aws: '#D42A1E', onprem: '#1E88D9', gcp: '#8CC152' };
const lineOf = (s: EnvSeries, key: keyof Sample): Line => ({
  key: s.env, label: s.kind === 'aws' ? 'AWS' : s.kind === 'onprem' ? '온프렘' : s.name, color: COLORS[s.kind] ?? '#7A6450',
  points: s.samples.map((x) => [x.t, x[key] as number | null]),
});

export function usePlanAnnotations() {
  const plan = useAtomValue(planAtom);
  const { events } = useEvents(plan);
  return events.filter((e) => e.event_type === 'state.changed' || e.event_type === 'execution.updated' || e.event_type === 'gate.completed')
    .map((e) => ({ t: Date.parse(e.occurred_at), label: e.event_type === 'state.changed' ? stageLabel(e.data?.to) : e.event_type }));
}

export function liveEnvs(store: ReturnType<typeof useLiveStore>) {
  return [...store.envs.values()].filter((s) => !s.error || s.samples.length);
}

export function TrafficPanels({ compact = false }: { compact?: boolean }) {
  const s = useLiveStore();
  const ann = usePlanAnnotations();
  const envs = liveEnvs(s);
  if (!envs.length) return <Empty />;
  const h = compact ? 120 : 160;
  return (
    <div className="grid grid-cols-1 gap-4 @3xl:grid-cols-2 @6xl:grid-cols-3">
      <LiveChart title="요청 수" unit="RPS" lines={envs.map((e) => lineOf(e, 'rps'))} format={(v) => v >= 100 ? v.toFixed(0) : v.toFixed(1)} height={h} annotations={ann} />
      <LiveChart title="p95 응답 시간" unit="ms" lines={envs.map((e) => ({ ...lineOf(e, 'p95'), points: lineOf(e, 'p95').points.map(([t, v]) => [t, v === null ? null : v * 1000]) }))}
        format={(v) => `${v.toFixed(1)}ms`} threshold={{ value: 200, label: 'SLO 200ms' }} height={h} annotations={ann} />
      <LiveChart title="오류율" unit="%" lines={envs.map((e) => ({ ...lineOf(e, 'err'), points: lineOf(e, 'err').points.map(([t, v]) => [t, v === null ? null : v * 100]) }))}
        format={(v) => `${v.toFixed(2)}%`} threshold={{ value: 1, label: '1%' }} height={h} annotations={ann} />
    </div>
  );
}

export function DataPanels() {
  const s = useLiveStore();
  const ann = usePlanAnnotations();
  const envs = liveEnvs(s);
  if (!envs.length) return <Empty />;
  return (
    <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-2">
      <LiveChart title="논리 복제 지연" unit="MiB" lines={envs.map((e) => ({ ...lineOf(e, 'lag'), points: lineOf(e, 'lag').points.map(([t, v]) => [t, v === null ? null : v / 1048576]) }))}
        format={(v) => `${v.toFixed(1)}MiB`} threshold={{ value: 64, label: '경보 64MiB' }} annotations={ann} />
      <LiveChart title="PgBouncer 클라이언트" unit="active · waiting" lines={envs.flatMap((e) => [lineOf(e, 'active'), { ...lineOf(e, 'waiting'), key: `${e.env}-w`, label: `${lineOf(e, 'waiting').label}·대기`, color: '#FFD23F' }])}
        format={(v) => v.toFixed(0)} annotations={ann} />
    </div>
  );
}

export function StatePanels() {
  const t = useT();
  const s = useLiveStore();
  const links = useConsoleLinks();
  return (
    <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-2">
      {[...s.envs.values()].map((e: any) => (
        <section key={e.env} className="panel p-3">
          <div className="mb-2 flex items-center gap-2">
            <span className="shrink-0 whitespace-nowrap rounded border-2 border-ink px-1.5 text-[10px] font-black" style={{ background: COLORS[e.kind] ?? '#eee', color: '#fff3dc' }}>{e.kind === 'aws' ? 'AWS' : e.kind === 'onprem' ? t('온프렘') : e.kind}</span>
            <b className="min-w-0 truncate">{e.name}</b>
            <span className="ml-auto whitespace-nowrap text-[11px] text-ink-soft">{e.error ? `${t('미관측')} (${e.error})` : `${t('수집')} ${e.lastSecondsAgo ?? '—'}${t('초 전')}`}</span>
            <button className="btn !px-1.5 !py-0 text-[10.5px]" title={t('이 환경의 콘솔을 내 계정으로 엽니다')} onClick={() => { const u = links.forKind(e.kind); if (u) window.open(u, '_blank'); }}>{t('콘솔')} ↗</button>
          </div>
          {e.error && !e.samples.length ? <p className="halftone rounded-lg border-2 border-dashed border-ink p-2 text-[12px]">{t('관측 신호가 없습니다. 미관측은 0이 아닙니다.')}</p> : (
            <table className="w-full text-[12px]">
              <tbody>
                {(e.db?.instances ?? []).map((i: any) => <tr key={i.pod} className="border-b border-dashed border-ink/20"><td className="py-1">DB {i.pod}</td><td>{i.role}</td><td className="text-right"><Dot ok={i.up} /></td></tr>)}
                {(e.db?.replication_slots ?? []).map((r: any) => <tr key={r.slot} className="border-b border-dashed border-ink/20"><td className="py-1 font-mono text-[11px]">{r.slot}</td><td>{(r.lag_bytes / 1048576).toFixed(1)}MiB</td><td className="text-right"><Dot ok={r.active} warn /></td></tr>)}
                {(e.pool?.poolers ?? []).map((p: any) => <tr key={p.pooler}><td className="py-1">{p.pooler}</td><td>{(p.databases ?? []).map((d: any) => `${d.name}${d.paused ? ' (멈춤)' : ''}`).join(', ')}</td><td className="text-right"><Dot ok={p.up} /></td></tr>)}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  );
}

function Dot({ ok, warn }: { ok: boolean; warn?: boolean }) {
  return <span className={`inline-block h-2.5 w-2.5 rounded-full border border-ink ${ok ? 'bg-wasabi' : warn ? 'bg-mustard' : 'bg-red'}`} />;
}

export function AlertStrip() {
  const t = useT();
  const s = useLiveStore();
  const firing = s.alerts.filter((a) => a.state === 'firing');
  if (!firing.length) return <div className="flex items-center gap-2 rounded-xl border-2 border-ink bg-[#e9f5da] px-3 py-1.5 text-[12px] font-bold">{t('경보 없음')}</div>;
  return (
    <div className="@container flex flex-col gap-1.5">
      {firing.map((a, i) => (
        <div key={i} className="flex items-center gap-2 rounded-xl border-2 border-ink bg-[#ffe3d6] px-3 py-1.5 text-[12px]">
          <span className="shrink-0 rounded bg-red px-1.5 text-[10px] font-black uppercase text-cream">{a.severity}</span>
          <b className="shrink-0 font-mono text-[11px]">{a.alertname}</b>
          <span className="min-w-0 flex-1 truncate">{a.summary}</span>
          <span className="hidden whitespace-nowrap text-[11px] text-ink-soft @3xl:inline">{a.environment} · {new Date(a.active_at).toLocaleTimeString()}~</span>
        </div>
      ))}
    </div>
  );
}

function Empty(): ReactNode {
  const t = useT();
  return <p className="halftone rounded-xl border-2 border-dashed border-ink p-3 text-[12.5px]">{t('관측 값을 기다리는 중입니다. 재생 중이면 첫 수집 뒤 그려집니다.')}</p>;
}
