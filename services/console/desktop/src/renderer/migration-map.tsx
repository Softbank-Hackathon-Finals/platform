// Migration map: where the move stands and whether traffic, data and control actually flow between the three
// clusters now. Grammar: architecture with containment (AWS EKS source · platform RKE2 · on-prem RKE2 target),
// named directed edges, four reading flows. Boxes are HTML (crisp at any width); edges are an SVG overlay
// measured from the boxes, so the map reflows instead of shrinking its text.
//
// Evidence ledger (element → source → claim ceiling) is the `src`/`ceiling` of every node and edge below and is
// shown beside the map. Values come from the observability API (1 m windows via SOVA MCP), the T10 plan and the
// T15 approval host; anything not observed renders as "미관측", never 0.
import { useQuery } from '@tanstack/react-query';
import { useAtomValue } from 'jotai';
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { MAIN_STAGES, stageLabel } from './brand';
import { useT } from './i18n';
import { useLiveStore } from './live';
import { IS_WEB, planAtom, sova, useTool } from './lib';

type Tone = 'ok' | 'warn' | 'fail' | 'standby' | 'idle';
interface Node { id: string; col: 'src' | 'plat' | 'dst' | 'top'; title: string; sub: string; value?: ReactNode; tone: Tone; badge?: string; src: string; ceiling: string }
interface Edge { id: string; from: string; to: string; kind: 'data' | 'control' | 'observe' | 'traffic'; label: string; value: string; tone: Tone; rate: number; dashed?: boolean; src: string; ceiling: string; chip?: string }
type Pt = { x: number; y: number };

/** Orthogonal polyline with rounded corners. */
function ortho(pts: Pt[], r = 9) {
  const len = (a: Pt, b: Pt) => Math.hypot(b.x - a.x, b.y - a.y);
  const toward = (from: Pt, to: Pt, d: number) => { const l = len(from, to) || 1; return { x: from.x + ((to.x - from.x) * d) / l, y: from.y + ((to.y - from.y) * d) / l }; };
  let d = `M${pts[0].x},${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [a, b, c] = [pts[i - 1], pts[i], pts[i + 1]];
    const k = Math.min(r, len(a, b) / 2, len(b, c) / 2), p = toward(b, a, k), q = toward(b, c, k);
    d += ` L${p.x},${p.y} Q${b.x},${b.y} ${q.x},${q.y}`;
  }
  const z = pts[pts.length - 1];
  return `${d} L${z.x},${z.y}`;
}

const FLOWS = [
  ['all', '전체', []],
  ['traffic', '트래픽', ['t-src', 't-dst', 'p-src', 'p-dst']],
  ['data', '데이터 복제', ['repl', 'reverse']],
  ['observe', '관측', ['tel-src', 'tel-dst']],
  ['control', '통제·승인', ['approve', 'collect', 'cut-src', 'cut-dst']],
] as const;

const TONE: Record<Tone, { stroke: string; chip: string; dot: string }> = {
  ok: { stroke: '#1B1B1B', chip: 'bg-[#e9f5da]', dot: 'bg-wasabi' },
  warn: { stroke: '#B8860B', chip: 'bg-mustard', dot: 'bg-mustard' },
  fail: { stroke: '#D42A1E', chip: 'bg-[#ffe3d6]', dot: 'bg-red' },
  standby: { stroke: '#7A6450', chip: 'bg-paper', dot: 'bg-soba' },
  idle: { stroke: '#9b8f84', chip: 'bg-[#f1ece4]', dot: 'bg-[#cfc6bb]' },
};
const KIND_COLOR = { traffic: '#1E88D9', data: '#D42A1E', observe: '#7A6450', control: '#1B1B1B' };

const mib = (b?: number | null) => (b === null || b === undefined ? '미관측' : b < 1024 * 1024 ? `${Math.round(b)}B` : `${(b / 1048576).toFixed(1)}MiB`);
const rps = (v?: number | null) => (v === null || v === undefined ? '미관측' : v >= 1000 ? `${(v / 1000).toFixed(1)}k rps` : `${v.toFixed(1)} rps`);

/** Writer side from the plan stage (design: SHIFT moves traffic, CUTOVER moves writes). Not a DB measurement. */
function writer(state?: string) {
  if (!state) return null;
  if (['FAILBACK', 'DONE_ROLLED_BACK'].includes(state)) return 'src';
  const i = MAIN_STAGES.findIndex(([k]) => k === state);
  const cut = MAIN_STAGES.findIndex(([k]) => k === 'CUTOVER');
  return i >= cut ? 'dst' : 'src';
}

export function MigrationMap() {
  const t = useT();
  const plan = useAtomValue(planAtom);
  const s = useLiveStore();
  const p = useTool('sova_get_plan', plan ? { plan_id: plan } : null, 10_000).data?.data;
  const run = useQuery({ queryKey: ['run-state', plan, p?.version], queryFn: () => sova.approval('run-state', plan), enabled: !!plan && !IS_WEB, refetchInterval: 15_000 }).data;
  const [flow, setFlow] = useState<string>('all');
  const [pick, setPick] = useState<string | null>(null);

  const env = (kind: string) => [...s.envs.values()].find((e: any) => e.kind === kind && e.samples.length) as any;
  const aws = env('aws'), onp = env('onprem');
  const last = (e: any) => e?.samples.at(-1);
  const slot = (e: any, name: string) => (e?.db?.replication_slots ?? []).find((x: any) => x.slot === name);
  const pool = (e: any) => (e?.pool?.poolers ?? [])[0];
  const w = writer(p?.state);
  const fwd = slot(aws, 't8_source_to_target'), rev = slot(onp, 't8_target_to_source');
  const cutting = p?.state === 'CUTOVER' || p?.state === 'FAILBACK';
  const stale = (e: any) => (e?.lastSecondsAgo ?? 999) > 60;

  const nodes: Node[] = [
    { id: 'users', col: 'top', title: t('사용자 · 부하'), sub: 'demo-chat', tone: 'idle', src: '관측 API http 뷰(두 환경 합)', ceiling: '요청 출처를 구분하지 않는다(부하 발생기 포함)' },
    { id: 'console', col: 'plat', title: t('SOVA 콘솔 (사람)'), sub: 'SSO bravo-operator', value: IS_WEB ? t('승인은 데스크톱 앱에서') : run ? (run.ok ? `${t('승인')} → ${run.body.state} · ${run.body.owner}` : `${t('승인')} HTTP ${run.status}`) : undefined, tone: IS_WEB ? 'standby' : !run ? 'idle' : run.ok ? 'ok' : 'fail', src: '이 앱 · T15 승인 호스트 run-state', ceiling: '사람 승인만 보낸다. 안내자는 승인 불가' },
    { id: 'src-app', col: 'src', title: 'demo-chat', sub: 'source-dev', value: rps(last(aws)?.rps), tone: !aws ? 'idle' : (last(aws)?.err ?? 0) > 0.01 ? 'fail' : 'ok', src: 'http · aws', ceiling: '1분 창 RPS·오류율·p95' },
    { id: 'src-pool', col: 'src', title: pool(aws)?.pooler ?? 'source-pgbouncer', sub: 'PgBouncer', value: pool(aws) ? `${last(aws)?.active ?? 0} active · ${last(aws)?.waiting ?? 0} wait` : undefined, tone: !aws ? 'idle' : pool(aws)?.up ? (pool(aws)?.databases ?? []).some((d: any) => d.paused) ? 'warn' : 'ok' : 'fail', badge: (pool(aws)?.databases ?? []).some((d: any) => d.paused) ? 'PAUSE' : undefined, src: 'pgbouncer · aws', ceiling: 'exporter 상태. 컷오버 판정 입력 아님' },
    { id: 'src-db', col: 'src', title: 'source-db', sub: 'CNPG · AWS EKS', value: aws?.db?.instances?.[0] ? `${aws.db.instances[0].role} · ${aws.db.instances[0].up ? 'up' : 'down'}` : undefined, tone: !aws ? 'idle' : aws.samples.at(-1)?.dbUp ? 'ok' : 'fail', badge: w === 'src' ? t('쓰기') : undefined, src: 'database · aws', ceiling: '쓰기 표시는 계획 단계 기준(DB 실측 아님)' },
    { id: 'plat-ctl', col: 'plat', title: t('워크플로 · 승인 · Executor'), sub: 'T10 · T15 · platform-state', value: p ? `${stageLabel(p.state)} v${p.version}` : t('계획 없음'), tone: !p ? 'idle' : ['SAFE_HOLD', 'NEEDS_HUMAN'].includes(p.state) ? 'fail' : p.state === 'AWAIT_APPROVAL' ? 'warn' : 'ok', badge: run?.ok ? `${t('진행')} ${run.body.owner}` : undefined, src: 'T10 계획 · 승인 호스트 run-state', ceiling: '상태 머신 단계. 실행 결과는 실행 기록에서' },
    { id: 'plat-obs', col: 'plat', title: t('관측 저장소 · 조회 API'), sub: 'bravo-observability', value: `${t('경보')} ${s.alerts.filter((a) => a.state === 'firing').length}`, tone: s.alerts.some((a) => a.state === 'firing') ? 'warn' : s.at ? 'ok' : 'idle', src: 'alerts 뷰', ceiling: '플랫폼 Prometheus 규칙 기준' },
    { id: 'dst-app', col: 'dst', title: 'demo-chat', sub: 'target-dev', value: rps(last(onp)?.rps), tone: !onp ? 'idle' : (last(onp)?.err ?? 0) > 0.01 ? 'fail' : 'ok', src: 'http · onprem', ceiling: '1분 창 RPS·오류율·p95' },
    { id: 'dst-pool', col: 'dst', title: pool(onp)?.pooler ?? 'target-pgbouncer', sub: 'PgBouncer', value: pool(onp) ? `${last(onp)?.active ?? 0} active · ${last(onp)?.waiting ?? 0} wait` : undefined, tone: !onp ? 'idle' : pool(onp)?.up ? (pool(onp)?.databases ?? []).some((d: any) => d.paused) ? 'warn' : 'ok' : 'fail', badge: (pool(onp)?.databases ?? []).some((d: any) => d.paused) ? 'PAUSE' : undefined, src: 'pgbouncer · onprem', ceiling: 'exporter 상태. 컷오버 판정 입력 아님' },
    { id: 'dst-db', col: 'dst', title: 'target-db', sub: 'CNPG · Mac mini RKE2', value: onp?.db?.instances?.[0] ? `${onp.db.instances[0].role} · ${onp.db.instances[0].up ? 'up' : 'down'}` : undefined, tone: !onp ? 'idle' : onp.samples.at(-1)?.dbUp ? 'ok' : 'fail', badge: w === 'dst' ? t('쓰기') : undefined, src: 'database · onprem', ceiling: '쓰기 표시는 계획 단계 기준(DB 실측 아님)' },
  ];
  const edges: Edge[] = [
    { id: 't-src', from: 'users', to: 'src-app', kind: 'traffic', label: t('요청'), value: rps(last(aws)?.rps), tone: aws ? 'ok' : 'idle', rate: last(aws)?.rps ?? 0, src: 'http · aws', ceiling: 'SHIFT 가중치가 아니라 관측된 요청량' },
    { id: 't-dst', from: 'users', to: 'dst-app', kind: 'traffic', label: t('요청'), value: rps(last(onp)?.rps), tone: onp ? 'ok' : 'idle', rate: last(onp)?.rps ?? 0, src: 'http · onprem', ceiling: '부하 발생기 요청 포함' },
    { id: 'p-src', from: 'src-app', to: 'src-pool', kind: 'traffic', label: 'SQL', value: `${last(aws)?.active ?? '—'} conn`, tone: aws ? 'ok' : 'idle', rate: (last(aws)?.active ?? 0) * 10, src: 'pgbouncer · aws', ceiling: '클라이언트 연결 수' },
    { id: 'p-dst', from: 'dst-app', to: 'dst-pool', kind: 'traffic', label: 'SQL', value: `${last(onp)?.active ?? '—'} conn`, tone: onp ? 'ok' : 'idle', rate: (last(onp)?.active ?? 0) * 10, src: 'pgbouncer · onprem', ceiling: '클라이언트 연결 수' },
    { id: 'repl', from: 'src-db', to: 'dst-db', kind: 'data', label: 't8_source_to_target', value: fwd ? `${fwd.active ? t('복제 중') : t('멈춤')} · ${t('지연')} ${mib(fwd.lag_bytes)}` : t('미관측'), tone: !fwd ? 'idle' : !fwd.active ? (w === 'src' ? 'fail' : 'standby') : fwd.lag_bytes > 64 * 1048576 ? 'warn' : 'ok', rate: fwd?.active ? 400 : 0, src: 'database · aws replication_slots', ceiling: '논리 복제 슬롯 상태·지연 바이트' },
    { id: 'reverse', from: 'dst-db', to: 'src-db', kind: 'data', label: 't8_target_to_source', value: rev ? `${rev.active ? t('복제 중') : t('되돌리기 대기')} · WAL ${mib(rev.lag_bytes)}` : t('미관측'), tone: !rev ? 'idle' : rev.active ? 'ok' : w === 'dst' ? 'fail' : 'standby', rate: rev?.active ? 400 : 0, dashed: !rev?.active, src: 'database · onprem replication_slots · t8-replication-health.md', ceiling: '컷오버 전 대기는 설계대로. 컷오버 뒤 꺼져 있으면 장애' },
    { id: 'tel-src', from: 'src-db', to: 'plat-obs', kind: 'observe', label: 'OTLP', value: aws ? `${Math.round(aws.lastSecondsAgo ?? 0)}${t('초 전')}` : t('미관측'), tone: !aws ? 'idle' : stale(aws) ? 'fail' : 'ok', rate: aws ? 60 : 0, src: 'last_received_seconds_ago · aws', ceiling: '마지막 수신 시각', chip: aws ? `OTLP · ${Math.round(aws.lastSecondsAgo ?? 0)}s` : undefined },
    { id: 'tel-dst', from: 'dst-db', to: 'plat-obs', kind: 'observe', label: 'OTLP', value: onp ? `${Math.round(onp.lastSecondsAgo ?? 0)}${t('초 전')}` : t('미관측'), tone: !onp ? 'idle' : stale(onp) ? 'fail' : 'ok', rate: onp ? 60 : 0, src: 'last_received_seconds_ago · onprem', ceiling: '마지막 수신 시각', chip: onp ? `OTLP · ${Math.round(onp.lastSecondsAgo ?? 0)}s` : undefined },
    { id: 'approve', from: 'console', to: 'plat-ctl', kind: 'control', label: t('사람 승인'), value: IS_WEB ? t('데스크톱 앱') : run ? (run.ok ? `${run.body.state} · ${run.body.owner}` : `HTTP ${run.status}`) : '—', tone: IS_WEB ? 'standby' : !run ? 'idle' : run.ok ? 'ok' : 'fail', rate: 0, src: 'T15 승인 호스트 run-state', ceiling: 'SSO 운영자 증명. 사전 위임이 있으면 호스트가 진행', chip: '' },
    { id: 'collect', from: 'plat-ctl', to: 'src-app', kind: 'control', label: t('읽기 수집'), value: t('쓰기 거부 확인'), tone: 'ok', rate: 0, src: 'T10 등록 영수증 checks(read_access verified, create_pods denied)', ceiling: '등록 시점의 권한 확인', chip: t('읽기 수집') },
    { id: 'cut-src', from: 'plat-ctl', to: 'src-pool', kind: 'control', label: 'PAUSE', value: cutting ? t('컷오버 중') : t('대기'), tone: cutting ? 'warn' : 'standby', rate: cutting ? 200 : 0, dashed: !cutting, src: 'T10 단계(CUTOVER)', ceiling: '단계 표시. Executor 실행 실측 아님', chip: `PAUSE · ${cutting ? t('진행') : t('대기')}` },
    { id: 'cut-dst', from: 'plat-ctl', to: 'dst-pool', kind: 'control', label: 'RESUME', value: cutting ? t('컷오버 중') : t('대기'), tone: cutting ? 'warn' : 'standby', rate: cutting ? 200 : 0, dashed: !cutting, src: 'T10 단계(CUTOVER)', ceiling: '단계 표시. Executor 실행 실측 아님', chip: `RESUME · ${cutting ? t('진행') : t('대기')}` },
  ];
  const active = FLOWS.find(([k]) => k === flow)![2] as readonly string[];
  const lit = (e: Edge) => flow === 'all' || active.includes(e.id);
  const litNode = (id: string) => flow === 'all' || edges.some((e) => lit(e) && (e.from === id || e.to === id));

  // ---- geometry: measure the HTML boxes, draw curves between their facing sides ----
  const box = useRef<HTMLDivElement>(null);
  const refs = useRef(new Map<string, HTMLElement>());
  const [geo, setGeo] = useState<Record<string, DOMRect>>({});
  const [size, setSize] = useState({ w: 0, h: 0 });
  const sig = useRef('');
  const measure = () => {
    const el = box.current; if (!el) return;
    const o = el.getBoundingClientRect(); const g: Record<string, DOMRect> = {};
    refs.current.forEach((n, id) => { const r = n.getBoundingClientRect(); g[id] = new DOMRect(r.x - o.x, r.y - o.y, r.width, r.height); });
    const next = JSON.stringify([o.width, o.height, Object.entries(g).map(([k, r]) => [k, r.x, r.y, r.width, r.height])]);
    if (next !== sig.current) { sig.current = next; setGeo(g); setSize({ w: o.width, h: o.height }); } // only on a real move
  };
  useLayoutEffect(measure); // boxes change size when values arrive
  useLayoutEffect(() => {
    const ro = new ResizeObserver(() => measure()); if (box.current) ro.observe(box.current);
    return () => ro.disconnect();
  }, []);
  // Orthogonal routes that never cross a box: neighbours in a row get a straight connector across the gap,
  // offset rows a short elbow inside the gap, a box above another column drops onto it from its side, and
  // DB ↔ DB replication runs in its own two lanes under the boundaries (it does not pass through the platform).
  const route = (e: Edge): { d: string; chip: Pt; maxW: number } | null => {
    const a = geo[e.from], b = geo[e.to]; if (!a || !b) return null;
    const cx = (r: DOMRect) => r.x + r.width / 2, cy = (r: DOMRect) => r.y + r.height / 2;
    const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
    if (e.id === 'repl' || e.id === 'reverse') {
      const floor = Math.max(...['src', 'plat', 'dst'].map((c) => (geo[`col-${c}`] ? geo[`col-${c}`].y + geo[`col-${c}`].height : 0)));
      const fwd = e.id === 'repl', lane = floor + (fwd ? 16 : 38), s = fwd ? a : b, t = fwd ? b : a;
      // Forward is the inner lane, the failback slot the outer one; their verticals sit side by side, so they never cross.
      const x1 = cx(s) + (fwd ? 12 : -12), x2 = cx(t) + (fwd ? -12 : 12);
      const pts = fwd ? [{ x: x1, y: s.y + s.height }, { x: x1, y: lane }, { x: x2, y: lane }, { x: x2, y: t.y + t.height }]
        : [{ x: x2, y: t.y + t.height }, { x: x2, y: lane }, { x: x1, y: lane }, { x: x1, y: s.y + s.height }];
      return { d: ortho(pts), chip: { x: x1 + (x2 - x1) * (fwd ? 0.32 : 0.68), y: lane }, maxW: 260 };
    }
    const apart = Math.abs(cx(b) - cx(a)) > (a.width + b.width) / 2;
    if (apart && a.y + a.height <= b.y) {
      // From the side of a box above (users), across the top, down into the inner part of the target's top edge.
      const dir = Math.sign(cx(b) - cx(a)), y = cy(a), x = b.x + b.width * (dir < 0 ? 0.72 : 0.28);
      const x0 = dir > 0 ? a.x + a.width : a.x;
      return { d: ortho([{ x: x0, y }, { x, y }, { x, y: b.y }]), chip: { x: (x0 + x) / 2, y }, maxW: Math.abs(x - x0) - 12 };
    }
    if (apart) {
      const dir = Math.sign(cx(b) - cx(a)), x1 = dir > 0 ? a.x + a.width : a.x, x2 = dir > 0 ? b.x : b.x + b.width;
      const lo = Math.max(a.y, b.y) + 10, hi = Math.min(a.y + a.height, b.y + b.height) - 10, gap = Math.abs(x2 - x1) - 6;
      if (hi >= lo) { const y = (lo + hi) / 2; return { d: ortho([{ x: x1, y }, { x: x2, y }]), chip: { x: (x1 + x2) / 2, y }, maxW: gap }; }
      const y1 = clamp(cy(b), a.y + 12, a.y + a.height - 12), y2 = clamp(cy(a), b.y + 12, b.y + b.height - 12), gx = (x1 + x2) / 2;
      return { d: ortho([{ x: x1, y: y1 }, { x: gx, y: y1 }, { x: gx, y: y2 }, { x: x2, y: y2 }]), chip: { x: gx, y: (y1 + y2) / 2 }, maxW: gap };
    }
    // Same column: a straight drop through the shared span (the approval line keeps clear of the boundary chip).
    const lo = Math.max(a.x, b.x), hi = Math.min(a.x + a.width, b.x + b.width), x = lo + (hi - lo) * (e.id === 'approve' ? 0.8 : 0.5);
    const down = cy(b) > cy(a), y1 = down ? a.y + a.height : a.y, y2 = down ? b.y : b.y + b.height;
    return { d: ortho([{ x, y: y1 }, { x, y: y2 }]), chip: { x, y: (y1 + y2) / 2 }, maxW: 200 };
  };
  const speed = (r: number) => (r <= 0 ? 0 : Math.max(0.35, 3.2 - Math.log10(r + 1) * 0.7)); // seconds per dash cycle

  const nodeEl = (n: Node) => (
    <button key={n.id} ref={(el) => { if (el) refs.current.set(n.id, el); else refs.current.delete(n.id); }}
      onClick={() => setPick(pick === n.id ? null : n.id)}
      className={`relative z-10 flex min-w-0 flex-col rounded-xl border-2 border-ink bg-[#fffaf0] px-2.5 py-1.5 text-left shadow-pop-sm transition-opacity ${litNode(n.id) ? '' : 'opacity-35'} ${pick === n.id ? 'ring-2 ring-mustard' : ''}`}>
      <span className="flex min-w-0 items-center gap-1.5">
        <span className={`h-2.5 w-2.5 shrink-0 rounded-full border border-ink ${TONE[n.tone].dot} ${n.tone === 'ok' ? 'live-dot' : ''}`} />
        <b className="min-w-0 truncate text-[12.5px]">{n.title}</b>
        {n.badge && <span className="ml-auto shrink-0 rounded bg-red px-1 text-[9.5px] font-black text-cream">{n.badge}</span>}
      </span>
      <span className="truncate font-mono text-[10px] text-ink-soft">{n.sub}</span>
      {n.value !== undefined && <span className="tnum truncate text-[12px] font-bold">{n.value}</span>}
    </button>
  );
  const col = (c: Node['col']) => nodes.filter((n) => n.col === c).map(nodeEl);
  const sel = pick ? nodes.find((n) => n.id === pick) ?? null : null;
  // "All" lists only what needs a look (warn/fail/standby); a flow lists its edges in reading order.
  const steps = flow === 'all' ? edges.filter((e) => e.tone === 'warn' || e.tone === 'fail' || e.tone === 'idle') : edges.filter(lit);
  const count = (tone: Tone) => edges.filter((e) => e.tone === tone).length;

  return (
    <section className="panel mat @container overflow-hidden">
      <header className="flex flex-wrap items-center gap-2 border-b-2 border-ink bg-[#fffaf0] px-4 py-2">
        <div className="min-w-0">
          <div className="kicker">MIGRATION MAP</div>
          <h2 className="font-kdisplay text-[15px] leading-tight">{t('이전 지도 — 지금 무엇이 흐르고 있나')}</h2>
        </div>
        <div className="ml-auto flex overflow-hidden rounded-lg border-2 border-ink shadow-pop-sm">
          {FLOWS.map(([k, l]) => (
            <button key={k} onClick={() => setFlow(k)} className={`border-r-2 border-ink px-2 py-1 text-[11.5px] font-bold last:border-r-0 ${flow === k ? 'bg-mustard' : 'bg-[#fffaf0] hover:bg-paper'}`}>{t(l)}</button>
          ))}
        </div>
      </header>
      <div className="grid grid-cols-1 @5xl:grid-cols-[1fr_280px]">
        <div className="scroll-thin min-w-0 overflow-x-auto">
        <div ref={box} className="relative min-w-[700px] p-4 pb-20">
          <svg className="pointer-events-none absolute inset-0" width={size.w} height={size.h} aria-hidden>
            <defs>
              {Object.entries(KIND_COLOR).map(([k, c]) => (
                <marker key={k} id={`arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill={c} /></marker>
              ))}
            </defs>
            {edges.map((e) => {
              const g = route(e); if (!g) return null;
              const on = lit(e), sp = speed(e.rate);
              return (
                <g key={e.id} opacity={on ? 1 : 0.15}>
                  <path d={g.d} fill="none" stroke="#fffaf0" strokeWidth={7} />
                  <path d={g.d} fill="none" stroke={e.tone === 'fail' ? '#D42A1E' : KIND_COLOR[e.kind]} strokeWidth={e.kind === 'data' ? 3.2 : 2.2}
                    strokeDasharray={e.dashed ? '5 6' : undefined} markerEnd={`url(#arrow-${e.kind})`} opacity={e.tone === 'idle' ? 0.45 : 1} />
                  {sp > 0 && on && <path d={g.d} fill="none" stroke={e.kind === 'data' ? '#FFD23F' : '#fffaf0'} strokeWidth={e.kind === 'data' ? 3.2 : 2.2} strokeLinecap="round"
                    strokeDasharray="2 16" className="flow-dash" style={{ animationDuration: `${sp}s` }} />}
                </g>
              );
            })}
          </svg>
          {edges.map((e) => {
            const g = route(e); if (!g || !lit(e) || e.chip === '') return null;
            return (
              <span key={e.id} title={`${e.label} · ${e.value} — ${e.src} · ${e.ceiling}`} style={{ left: g.chip.x, top: g.chip.y, maxWidth: Math.max(44, g.maxW) }}
                className={`pointer-events-auto absolute z-20 -translate-x-1/2 -translate-y-1/2 truncate whitespace-nowrap rounded-md border-[1.5px] border-ink px-1.5 py-px text-[10.5px] font-bold shadow-[1px_1px_0_#1b1b1b] ${TONE[e.tone].chip}`}>
                {e.chip ?? <>{e.label} · <span className="tnum">{e.value}</span></>}
              </span>
            );
          })}
          {/* Users sit above the platform so each request line drops onto its own side; the person's console stands
              outside the platform boundary, directly above the workflow it approves. */}
          <div className="relative grid grid-cols-3 items-start gap-x-[clamp(84px,11cqw,132px)] gap-y-7">
            <div className="col-start-2 row-start-1 px-[8%]">{nodeEl(nodes.find((n) => n.id === 'users')!)}</div>
            {([['src', 'AWS EKS · 출발지', 'bg-red text-cream left-3', 'col-start-1'], ['plat', '플랫폼 RKE2 (AWS)', 'bg-ink text-cream left-3', 'col-start-2'], ['dst', '온프렘 RKE2 · 목적지', 'bg-cold text-white right-3', 'col-start-3']] as const).map(([c, l, chip, place]) => {
              const boundary = (
                <div ref={(el) => { if (el) refs.current.set(`col-${c}`, el); else refs.current.delete(`col-${c}`); }}
                  className="relative flex flex-col gap-7 rounded-2xl border-2 border-dashed border-ink/40 bg-[#fffaf0]/50 p-3 pt-6">
                  <span className={`absolute -top-2.5 rounded-md border-2 border-ink px-1.5 text-[10.5px] font-black ${chip}`}>{t(l)}</span>
                  {col(c).filter((el) => el.key !== 'console')}
                </div>
              );
              return c === 'plat'
                ? <div key={c} className={`${place} row-start-2 flex flex-col gap-7`}>{nodeEl(nodes.find((n) => n.id === 'console')!)}{boundary}</div>
                : <div key={c} className={`${place} row-start-2`}>{boundary}</div>;
            })}
          </div>
        </div>
        </div>
        <aside className="border-t-2 border-ink bg-[#fffaf0] p-3 text-[11.5px] @5xl:border-l-2 @5xl:border-t-0">
          {sel ? (
            <>
              <div className="kicker">{t('근거')}</div>
              <b className="text-[13px]">{sel.title}</b>
              <p className="mt-1"><span className="text-ink-soft">{t('출처')}</span> {sel.src}</p>
              <p><span className="text-ink-soft">{t('주장 한계')}</span> {sel.ceiling}</p>
            </>
          ) : (
            <>
              <div className="kicker">{flow === 'all' ? `${t('정상')} ${count('ok')} · ${t('대기(설계)')} ${count('standby')} · ${t('살펴볼 연결')} ${steps.length}` : t('읽는 순서')}</div>
              {flow === 'all' && !steps.length && <p className="mt-1 font-semibold">{t('지금 끊기거나 밀린 연결은 없습니다.')}</p>}
              <ol className="mt-1 flex flex-col gap-1.5">
                {steps.map((e, i) => (
                  <li key={e.id} className="flex gap-1.5">
                    <span className={`mt-1 h-2 w-2 shrink-0 rounded-full border border-ink ${TONE[e.tone].dot}`} />
                    <span className="min-w-0"><b>{flow === 'all' ? '' : `${i + 1}. `}{e.label}</b> <span className="tnum">{e.value}</span>
                      <span className="block text-[10.5px] text-ink-soft">{e.ceiling}</span></span>
                  </li>
                ))}
              </ol>
            </>
          )}
          <p className="mt-3 border-t-2 border-dashed border-ink/25 pt-2 text-[10.5px] text-ink-soft">
            {t('선 굵기: 데이터 > 요청·통제. 흐르는 점의 속도는 관측량. 점선은 대기·비활성. 값이 없으면 미관측(0 아님).')}
          </p>
        </aside>
      </div>
    </section>
  );
}
