import { useQuery, useQueryClient } from '@tanstack/react-query';
import hero from '../../assets/sova-hero.jpg';
import { useAtom, useAtomValue, useSetAtom } from 'jotai';
import { ArrowRight, ExternalLink, Hand, LogIn, PlugZap, Sparkles } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { EFFORT_LABEL, type NextAction, type Settings } from '../shared/types';
import { useModels } from './agent';
import { useT } from './i18n';
import { useConsoleLinks } from './settings';
import { Burst, MAIN_STAGES, Sparkline, Stamp, StageRail, StatusBadge, stageLabel } from './brand';
import { ago, callTool, explain, planAtom, sova, useTool, viewAtom } from './lib';
import { useLink } from './shell';

/* ---------- shared bits ---------- */

function Panel({ title, kicker, right, children, className = '' }: { title: string; kicker?: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel flex min-w-0 flex-col ${className}`}>
      <header className="flex items-center gap-2 border-b-2 border-ink px-4 py-2.5">
        <div className="min-w-0 flex-1">
          {kicker && <div className="kicker">{kicker}</div>}
          <h2 className="truncate font-kdisplay text-[15px] leading-tight">{title}</h2>
        </div>
        {right}
      </header>
      <div className="min-h-0 flex-1">{children}</div>
    </section>
  );
}

function Problem({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="halftone m-3 flex items-start gap-3 rounded-xl border-2 border-dashed border-ink p-3 text-[12.5px]">
      <Hand size={18} className="mt-0.5 shrink-0" />
      <div className="flex-1">{explain(error)}</div>
      {onRetry && <button className="btn !py-0.5 text-[11px]" onClick={onRetry}>다시 조회</button>}
    </div>
  );
}

function Tile({ label, value, sub, tone = 'bg-[#fffaf0]', children }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string; children?: ReactNode }) {
  return (
    <div className={`panel flex min-w-0 flex-col justify-between gap-2 p-3.5 ${tone}`}>
      <div className="kicker">{label}</div>
      <div className="num truncate text-[26px] leading-none">{value}</div>
      {children}
      {sub && <div className="truncate text-[11.5px] text-ink-soft">{sub}</div>}
    </div>
  );
}

const EVENT_TONE: Record<string, string> = {
  'state.changed': 'bg-cold', 'approval.requested': 'bg-mustard', 'approval.decided': 'bg-wasabi',
  'gate.completed': 'bg-soba', 'execution.updated': 'bg-red', 'failback.recorded': 'bg-tray',
};
const EVENT_LABEL: Record<string, string> = {
  'state.changed': '단계 변경', 'approval.requested': '승인 요청', 'approval.decided': '승인 결정',
  'gate.completed': '원본 대조', 'execution.updated': '실행', 'failback.recorded': '복귀',
};
function eventLine(e: any) {
  const d = e.data ?? {};
  if (e.event_type === 'state.changed') return `${stageLabel(d.from ?? d.previous_state)} → ${stageLabel(d.to ?? d.state)}`;
  if (e.event_type === 'gate.completed') return `판정 ${d.decision ?? '—'} · ${d.gate_id ?? ''}`;
  if (e.event_type === 'approval.decided') return `${d.approval_kind ?? ''} ${d.decision ?? '결정'}`.trim();
  if (e.event_type === 'execution.updated') return `${d.step} · ${d.status}${d.elapsed_ms ? ` · ${d.elapsed_ms}ms` : ''}`;
  return Object.entries(d).slice(0, 3).map(([k, v]) => `${k}: ${typeof v === 'object' ? '…' : v}`).join(' · ');
}

/** Follows a plan's event stream by cursor; only new events are fetched after the first read. */
export function useEvents(plan: string) {
  const [events, setEvents] = useState<any[]>([]);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let stop = false, cursor: string | null = null, timer: ReturnType<typeof setTimeout>;
    setEvents([]); setError(null);
    const tick = async () => {
      try {
        const r = await callTool('sova_get_progress', { plan_id: plan, since: cursor, limit: 200 });
        if (stop) return;
        cursor = r.cursor;
        if (r.data.events.length) setEvents((all) => [...all, ...r.data.events]);
        setError(null);
        timer = setTimeout(tick, r.data.events.length === 200 ? 0 : r.poll_after_ms ?? 15000);
      } catch (e) { if (!stop) { setError(e); timer = setTimeout(tick, 15000); } }
    };
    if (plan) void tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [plan]);
  return { events, error };
}

function EventStream({ events, compact = false }: { events: any[]; compact?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const list = [...events].reverse();
  if (!list.length) return <p className="p-4 text-[12.5px] text-ink-soft">아직 이벤트가 없습니다.</p>;
  return (
    <ol className="scroll-thin max-h-full overflow-y-auto py-1">
      {list.slice(0, compact ? 12 : 500).map((e) => (
        <li key={e.event_id} className="border-b border-dashed border-ink/20 last:border-0">
          <button onClick={() => setOpen(open === e.event_id ? null : e.event_id)} className="flex w-full items-center gap-3 px-4 py-2 text-left hover:bg-paper">
            <span className={`h-7 w-1.5 shrink-0 rounded-full border border-ink ${EVENT_TONE[e.event_type] ?? 'bg-cream'}`} />
            <span className="w-[74px] shrink-0 text-[11px] font-bold">{EVENT_LABEL[e.event_type] ?? e.event_type}</span>
            <span className="min-w-0 flex-1 truncate text-[12.5px]">{eventLine(e)}</span>
            <span className="shrink-0 font-mono text-[10.5px] text-ink-soft">#{e.sequence} · {ago(e.occurred_at)}</span>
          </button>
          {open === e.event_id && (
            <pre data-selectable className="mx-4 mb-2 max-h-56 overflow-auto rounded-lg border-2 border-ink bg-ink p-3 font-mono text-[11px] text-cream">
              {JSON.stringify(e, null, 2)}
            </pre>
          )}
        </li>
      ))}
    </ol>
  );
}

function NextActions({ actions, ask }: { actions: NextAction[]; ask: (q: string) => void }) {
  const [approval, setApproval] = useState<NextAction | null>(null);
  if (!actions.length) return <p className="p-4 text-[12.5px] text-ink-soft">다음 행동이 없습니다.</p>;
  return (
    <ul className="flex flex-col gap-2 p-3">
      {actions.map((a, i) => {
        const human = a.requires === 'human_approval';
        return (
          <li key={i} className={`rounded-xl border-2 border-ink p-2.5 ${human ? 'bg-mustard' : 'bg-[#fffaf0]'}`}>
            <div className="flex items-center gap-2">
              {human ? <span className="shrink-0 whitespace-nowrap rounded bg-red px-1.5 text-[10px] font-black text-cream">사람</span> : <span className="shrink-0 whitespace-nowrap rounded bg-cold px-1.5 text-[10px] font-black text-white">AI 조회</span>}
              <span className="min-w-0 truncate font-mono text-[12px] font-bold">{a.action}</span>
              <div className="flex-1" />
              {human
                ? <button className="btn !py-0.5 text-[11px]" onClick={() => setApproval(a)}>확인할 점</button>
                : <button className="btn !py-0.5 text-[11px]" onClick={() => ask(`${a.tool}로 "${a.action}"을 해 줘`)}>안내자에게 <ArrowRight size={12} /></button>}
            </div>
            {a.why && <p className="mt-1.5 text-[12px] leading-snug">{a.why}</p>}
          </li>
        );
      })}
      {approval && (
        <li className="rounded-xl border-2 border-dashed border-ink bg-paper p-3 text-[12px] leading-relaxed">
          <b>사람이 결정합니다.</b> 승인 전 확인: 계획 버전과 digest가 리허설·원본 대조 때와 같은지, 대조 판정이 PASS인지, 만료 시각이 남았는지.
          이 앱은 승인 요청을 보내지 않습니다. 승인은 T10 승인 경로(운영 콘솔)에서 결속 값과 함께 합니다.
          {approval.console_url && <div className="mt-1.5 font-mono text-[11px] text-ink-soft" data-selectable>{approval.console_url}</div>}
          <button className="btn mt-2 !py-0.5 text-[11px]" onClick={() => setApproval(null)}>닫기</button>
        </li>
      )}
    </ul>
  );
}

/** Live samples of one environment's HTTP view; the API returns the current 1 m window only. */
function useHttpSeries(environment: string) {
  const q = useTool('sova_get_observability', { environment_id: environment, view: 'http' }, 15_000);
  const series = useRef<{ rps: number[]; err: number[]; p95: number[] }>({ rps: [], err: [], p95: [] });
  const seen = useRef<string>('');
  const d = q.data?.data;
  if (d && d.queried_at !== seen.current) {
    seen.current = d.queried_at;
    const push = (k: 'rps' | 'err' | 'p95', v: number | null) => { if (v !== null && v !== undefined) series.current[k] = [...series.current[k], v].slice(-40); };
    push('rps', d.requests_per_second); push('err', d.error_ratio); push('p95', d.p95_seconds);
  }
  return { q, d, s: series.current };
}

function EnvMetrics({ env }: { env: any }) {
  const links = useConsoleLinks();
  const { q, d, s } = useHttpSeries(env.environment_id);
  const unobserved = q.isError || d?.state === 'UNOBSERVABLE';
  const fmt = (v: number | null | undefined, f: (n: number) => string) => (v === null || v === undefined ? '미관측' : f(v));
  return (
    <div className="panel flex flex-col gap-2 p-3.5">
      <div className="flex items-center gap-2">
        <span className={`rounded border-2 border-ink px-1.5 text-[10px] font-black ${env.roles?.includes('target') ? 'bg-cold text-white' : 'bg-red text-cream'}`}>
          {env.roles?.includes('target') ? '목적지' : '출발지'}</span>
        <span className="truncate font-bold">{env.name ?? env.environment_id}</span>
        <span className="font-mono text-[10.5px] text-ink-soft">{env.kind}</span>
        <div className="flex-1" />
        {d && <span className="text-[10.5px] text-ink-soft">수신 {d.last_received_seconds_ago ?? '—'}초 전</span>}
        <button className="btn !px-1.5 !py-0 text-[10.5px]" title="이 환경의 콘솔을 내 계정으로 엽니다" onClick={() => { const u = links.forKind(env.kind); if (u) window.open(u, '_blank'); }}>콘솔 ↗</button>
      </div>
      {unobserved ? (
        <div className="halftone rounded-lg border-2 border-dashed border-ink p-2.5 text-[12px]">{q.isError ? explain(q.error) : '관측 신호가 없습니다(미관측은 0이 아닙니다).'}</div>
      ) : (
        <div className="grid grid-cols-3 gap-2">
          {[['RPS', fmt(d?.requests_per_second, (n) => n.toFixed(1)), s.rps, '#1E88D9'],
            ['오류율', fmt(d?.error_ratio, (n) => (n * 100).toFixed(2) + '%'), s.err, '#D42A1E'],
            ['p95', fmt(d?.p95_seconds, (n) => Math.round(n * 1000) + 'ms'), s.p95, '#7A6450']].map(([l, v, vals, c]) => (
            <div key={l as string} className="rounded-lg border-2 border-ink bg-cream p-2">
              <div className="kicker">{l as string}</div>
              <div className="num text-[18px]">{v as string}</div>
              <Sparkline values={vals as number[]} width={110} height={26} color={c as string} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- views ---------- */

export function Dashboard({ ask }: { ask: (q: string) => void }) {
  const t = useT();
  const plan = useAtomValue(planAtom);
  const ctx = useTool('sova_get_context', {}, 30_000);
  const planQ = useTool('sova_get_plan', plan ? { plan_id: plan } : null, 10_000);
  const compare = useTool('sova_compare', plan ? { plan_id: plan } : null, 30_000);
  const exec = useTool('sova_get_execution', plan ? { plan_id: plan } : null, 15_000);
  const { events, error: evError } = useEvents(plan);
  const p = planQ.data?.data;
  const envs: any[] = ctx.data?.data?.environments ?? [];
  const workloads: any[] = ctx.data?.data?.workloads ?? [];

  if (ctx.isError) return <div className="p-6"><Problem error={ctx.error} onRetry={() => void ctx.refetch()} /></div>;
  if (ctx.data && !ctx.data.data.plans.length) return <EmptyStart ask={ask} envs={envs.length} workloads={workloads.length} />;

  const humanCount = planQ.data?.next_actions.filter((a) => a.requires === 'human_approval').length ?? 0;
  return (
    <div className="scroll-thin h-full overflow-y-auto overflow-x-hidden p-5">
      <div className="grid grid-cols-12 gap-4">
        <section className="panel mat col-span-12 overflow-hidden">
          <div className="flex items-center gap-3 border-b-2 border-ink bg-[#fffaf0] px-4 py-2.5">
            <div className="kicker">이전 계획</div>
            <span data-selectable className="max-w-[300px] truncate font-mono text-[12.5px] font-bold">{plan || '—'}</span>
            {p && <span className="min-w-0 flex-1 truncate text-[12px] text-ink-soft">{p.workload_id} → {p.target_environment_id} · v{p.version} · {ago(p.updated_at)}</span>}
            <span className="hidden whitespace-nowrap text-[11px] text-ink-soft 2xl:inline">단계는 워크플로 호스트가 진행합니다 · 빨간 고리는 사람 결정</span>
          </div>
          {planQ.isError ? <Problem error={planQ.error} /> : <StageRail state={p?.state} />}
        </section>

        <div className="col-span-12 grid grid-cols-4 gap-4">
          <Tile label={t('현재 단계')} value={stageLabel(p?.state)} sub={p ? `버전 ${p.version} · 마지막 이벤트 ${p.last_event_id?.split(':')[1] ?? '—'}번` : '—'} tone="bg-mustard" />
          <Tile label={t('원본 대조 게이트')} value={compare.data ? <StatusBadge status={compare.data.status} /> : '—'}
            sub={compare.data?.summary ?? (compare.isError ? explain(compare.error) : '조회 중')} />
          <Tile label={t('실행 기록')} value={exec.data ? `${exec.data.data.records.length}건` : '—'} sub={exec.data?.summary ?? '—'} />
          <Tile label={t('사람 결정 대기')} value={<span className={humanCount ? 'text-red' : ''}>{humanCount}</span>}
            sub={humanCount ? '승인·실행은 사람이 합니다' : '지금은 자동 진행 구간'} tone={humanCount ? 'bg-[#ffe3d6]' : 'bg-[#fffaf0]'} />
        </div>

        <Panel className="col-span-7 h-[380px]" kicker="EVENT STREAM" title={t('진행 이벤트')} right={<span className="text-[11px] text-ink-soft">{events.length}건 · cursor 추적</span>}>
          {evError ? <Problem error={evError} /> : <EventStream events={events} compact />}
        </Panel>
        <Panel className="col-span-5 h-[380px]" kicker="NEXT ACTIONS" title={t('다음 행동 (서버 계산)')}>
          <div className="scroll-thin h-full overflow-y-auto"><NextActions actions={planQ.data?.next_actions ?? []} ask={ask} /></div>
        </Panel>

        <div className="col-span-12 flex items-center gap-2 pt-1"><span className="font-kdisplay text-[16px]">{t('환경 관측')}</span><span className="kicker">HTTP · 1m 창 · 15초마다</span></div>
        <div className="col-span-12 grid grid-cols-2 gap-4">
          {envs.map((e) => <EnvMetrics key={e.environment_id} env={e} />)}
        </div>

        <Panel className="col-span-6" kicker="REGISTRY" title={`등록된 환경 ${envs.length}`}>
          <Table rows={envs} cols={[['environment_id', 'ID'], ['name', '이름'], ['kind', '종류'], ['roles', '역할'], ['registered_at', '등록']]} />
        </Panel>
        <Panel className="col-span-6" kicker="REGISTRY" title={`등록된 앱 ${workloads.length}`}>
          <Table rows={workloads} cols={[['workload_id', 'ID'], ['name', '이름'], ['namespace', '네임스페이스'], ['source_environment_id', '출발지'], ['registered_at', '등록']]} />
        </Panel>
      </div>
    </div>
  );
}

function Table({ rows, cols }: { rows: any[]; cols: [string, string][] }) {
  if (!rows.length) return <p className="p-4 text-[12.5px] text-ink-soft">없음</p>;
  return (
    <table className="w-full border-collapse text-[12px]">
      <thead><tr>{cols.map(([, l]) => <th key={l} className="kicker border-b-2 border-ink px-3 py-1.5 text-left !text-[9.5px]">{l}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-b border-dashed border-ink/20 last:border-0">
            {cols.map(([k]) => (
              <td key={k} data-selectable className={`max-w-[200px] truncate px-3 py-1.5 ${k.endsWith('_id') ? 'font-mono text-[11px]' : ''}`}>
                {k.endsWith('_at') ? ago(r[k]) : Array.isArray(r[k]) ? r[k].join(', ') : String(r[k] ?? '—')}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EmptyStart({ ask, envs, workloads }: { ask: (q: string) => void; envs: number; workloads: number }) {
  const setView = useSetAtom(viewAtom);
  return (
    <div className="sunburst-soft grid h-full place-items-center p-8">
      <div className="panel max-w-[620px] p-7 !shadow-pop-lg">
        <div className="flex items-start gap-5">
          <Burst className="h-28 w-28 shrink-0 text-[17px]">無停止<br />移行</Burst>
          <div>
            <h2 className="font-kdisplay text-[26px] leading-tight">아직 이전 계획이 없어요</h2>
            <p className="mt-2 text-[13.5px] leading-relaxed">
              SOVA는 운영 중인 서비스를 멈추지 않고 옮깁니다. 출발지·목적지 환경과 옮길 앱을 등록하면 계획을 만들 수 있어요.
              지금 등록된 환경 {envs}개, 앱 {workloads}개.
            </p>
            <div className="mt-4 flex gap-2">
              <button className="btn btn-red" onClick={() => ask('SOVA가 처음이에요. 무엇부터 하면 되는지 지금 상태를 보고 알려 주세요.')}><Sparkles size={15} /> 안내자와 시작</button>
              <button className="btn" onClick={() => setView('guide')}>8단계 먼저 보기</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Plans() {
  const [plan, setPlan] = useAtom(planAtom);
  const setView = useSetAtom(viewAtom);
  const q = useTool('sova_list_plans', {}, 30_000);
  const rows: any[] = q.data?.data ?? [];
  return (
    <div className="scroll-thin h-full overflow-y-auto p-5">
      <Panel kicker="PLANS" title={`이전 계획 ${rows.length}`}>
        {q.isError ? <Problem error={q.error} onRetry={() => void q.refetch()} /> : (
          <ul>
            {rows.map((r) => {
              const idx = MAIN_STAGES.findIndex(([k]) => k === r.state);
              return (
                <li key={r.plan_id}>
                  <button onClick={() => { setPlan(r.plan_id); setView('dashboard'); }}
                    className={`flex w-full items-center gap-4 border-b border-dashed border-ink/20 px-4 py-3 text-left hover:bg-paper ${r.plan_id === plan ? 'bg-paper' : ''}`}>
                    <span className="font-mono text-[12.5px] font-bold">{r.plan_id}</span>
                    <span className="flex h-2.5 w-48 overflow-hidden rounded-full border-2 border-ink bg-cream">
                      <span className="bg-soba" style={{ width: `${Math.max(0, idx + 1) / MAIN_STAGES.length * 100}%` }} />
                    </span>
                    <span className="font-kdisplay text-[13px]">{stageLabel(r.state)}</span>
                    <span className="text-[11.5px] text-ink-soft">v{r.version}</span>
                    <div className="flex-1" />
                    <span className="text-[11.5px] text-ink-soft">{ago(r.updated_at)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
    </div>
  );
}

export function Timeline() {
  const plan = useAtomValue(planAtom);
  const { events, error } = useEvents(plan);
  return (
    <div className="h-full p-5">
      <Panel className="h-full" kicker="TIMELINE" title={`계획 ${plan || '—'}의 전체 이벤트`} right={<span className="text-[11px] text-ink-soft">{events.length}건 · 눌러서 원문</span>}>
        {error ? <Problem error={error} /> : <EventStream events={events} />}
      </Panel>
    </div>
  );
}

function SecuritySummary({ env }: { env: any }) {
  const q = useTool('sova_get_security', { environment_id: env.environment_id, view: 'summary' }, 60_000);
  return (
    <div className="panel p-3.5">
      <div className="mb-2 flex items-center gap-2"><span className="font-bold">{env.name ?? env.environment_id}</span><span className="kicker">보안 요약</span></div>
      {q.isError ? <div className="halftone rounded-lg border-2 border-dashed border-ink p-2.5 text-[12px]">{explain(q.error)}</div>
        : <pre data-selectable className="scroll-thin max-h-48 overflow-auto rounded-lg bg-paper p-2 font-mono text-[11px]">{JSON.stringify(q.data?.data ?? {}, null, 2)}</pre>}
    </div>
  );
}

export function Observe() {
  const ctx = useTool('sova_get_context', {});
  const envs: any[] = ctx.data?.data?.environments ?? [];
  return (
    <div className="scroll-thin h-full overflow-y-auto p-5">
      <div className="mb-3 flex items-center gap-2"><span className="font-kdisplay text-[16px]">HTTP 관측</span><span className="kicker">RPS · 오류율 · p95</span></div>
      <div className="grid grid-cols-2 gap-4">{envs.map((e) => <EnvMetrics key={e.environment_id} env={e} />)}</div>
      <div className="mb-3 mt-6 flex items-center gap-2"><span className="font-kdisplay text-[16px]">보안 발견</span><span className="kicker">고객 환경의 비신뢰 데이터</span></div>
      <div className="grid grid-cols-2 gap-4">{envs.map((e) => <SecuritySummary key={e.environment_id} env={e} />)}</div>
    </div>
  );
}

export function Guide({ ask }: { ask: (q: string) => void }) {
  const flow = useQuery({ queryKey: ['res', 'flow'], queryFn: async () => JSON.parse((await sova.resource('sova://guide/flow')) ?? '[]') });
  const glossary = useQuery({ queryKey: ['res', 'glossary'], queryFn: async () => JSON.parse((await sova.resource('sova://glossary')) ?? '{}') });
  const steps: any[] = flow.data ?? [];
  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <div className="sunburst relative overflow-hidden border-b-2 border-ink px-8 py-8">
        <img src={hero} alt="" className="pointer-events-none absolute -right-10 top-0 h-full opacity-95" style={{ maskImage: 'linear-gradient(90deg, transparent, #000 30%)' }} />
        <div className="relative max-w-[560px]">
          <div className="wordmark text-[44px]">처음 오셨나요?</div>
          <p className="mt-4 inline-block rounded-xl border-2 border-ink bg-cream px-4 py-2.5 font-kdisplay text-[17px] shadow-pop">止めない。こぼさない。味はそのまま。</p>
          <p className="mt-3 max-w-[480px] text-[13.5px] font-semibold text-cream [text-shadow:1px_1px_0_#1b1b1b]">
            멈추지 않고, 흘리지 않고, 맛은 그대로. 운영 중인 서비스를 클라우드 밖으로 옮기는 여덟 단계입니다. 사람이 결정하는 곳에는 빨간 표시가 있어요.
          </p>
          <button className="btn btn-mustard mt-4" onClick={() => ask('SOVA가 처음이에요. 8단계를 쉽게 설명하고 지금 우리 상태에서 첫 행동을 알려 주세요.')}><Sparkles size={15} /> 안내자에게 설명 듣기</button>
        </div>
      </div>
      <div className="grid grid-cols-4 gap-4 p-6">
        {flow.isError || (!flow.isLoading && !steps.length) ? <div className="col-span-4"><Problem error={new Error('안내 자료를 읽지 못했습니다. 연결 설정을 확인하세요.')} /></div> : null}
        {steps.map((s) => (
          <div key={s.step} className="panel relative flex flex-col gap-2 p-4">
            <span className="absolute -left-2 -top-3 grid h-9 w-9 place-items-center rounded-full border-2 border-ink bg-mustard font-display text-[15px] shadow-pop-sm">{s.step}</span>
            <div className="mt-1 flex items-center gap-2"><h3 className="font-kdisplay text-[17px]">{s.name}</h3>
              {/사람|확인|선택/.test(s.human) && <span className="rounded bg-red px-1.5 text-[10px] font-black text-cream">사람</span>}</div>
            <p className="text-[12.5px] leading-relaxed">{s.purpose}</p>
            <div className="mt-auto space-y-1 border-t-2 border-dashed border-ink/30 pt-2 text-[11.5px]">
              <div><b>완료</b> {s.done}</div>
              <div><b>사람</b> {s.human}</div>
              <div className="font-mono text-[10.5px] text-ink-soft">{s.tools.join(' · ')}</div>
            </div>
          </div>
        ))}
      </div>
      <div className="px-6 pb-8">
        <Panel kicker="GLOSSARY" title="용어집">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 p-4">
            {Object.entries(glossary.data ?? {}).map(([k, v]) => (
              <div key={k} className="border-b border-dashed border-ink/20 pb-2"><dt className="font-bold">{k}</dt><dd className="text-[12.5px] text-ink-soft">{v as string}</dd></div>
            ))}
          </dl>
        </Panel>
      </div>
    </div>
  );
}

export function SettingsView() {
  const qc = useQueryClient();
  const link = useLink();
  const [s, setS] = useState<Settings | null>(null);
  const [msg, setMsg] = useState('');
  useEffect(() => { void sova.settings().then(setS); }, []);
  if (!s) return null;
  const save = async (patch: Partial<Settings>) => {
    try { setS(await sova.saveSettings(patch)); setMsg('저장했습니다.'); await qc.invalidateQueries(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div className="scroll-thin h-full overflow-y-auto p-5">
      <div className="grid max-w-[920px] grid-cols-2 gap-4">
        <Panel className="col-span-2" kicker="CONNECTION" title="SOVA MCP 연결"
          right={<span className={`rounded border-2 border-ink px-2 py-0.5 text-[11px] font-black ${link.data?.connected ? 'bg-wasabi' : 'bg-red text-cream'}`}>{link.data?.connected ? '연결됨' : '미연결'}</span>}>
          <ConnectionForm s={s} save={save} />
          <div className="flex flex-wrap items-center gap-2 border-t-2 border-dashed border-ink/30 px-4 py-3">
            <button className="btn" onClick={async () => setMsg((await sova.ssoLogin()).message)}><LogIn size={14} /> SSO 로그인</button>
            <button className="btn" onClick={() => void link.refetch()}><PlugZap size={14} /> 연결 시험</button>
            <span className="text-[12px] text-ink-soft">{msg || (link.data?.code ? explain({ code: link.data.code }) : link.data ? `도구 ${link.data.tools.length}개 · ${ago(link.data.checkedAt)}` : '')}</span>
          </div>
        </Panel>
        <Panel kicker="MODE" title="연결 범위">
          <div className="flex flex-col gap-3 p-4">
            <label className="flex items-center gap-3">
              <Stamp size={36}>{s.readOnly ? '冷' : '書'}</Stamp>
              <span className="flex-1 text-[12.5px] leading-snug"><b>{s.readOnly ? '읽기 전용' : '등록·계획 생성 허용'}</b><br />
                <span className="text-ink-soft">쓰기를 허용해도 각 등록은 미리보기와 확인 창을 거칩니다. 승인·실행은 열리지 않습니다.</span></span>
              <input type="checkbox" className="h-5 w-5 accent-[#d42a1e]" checked={!s.readOnly} onChange={(e) => void save({ readOnly: !e.target.checked })} />
            </label>
            <Field label="계획 범위 고정 (선택)" value={s.planScope} onSave={(v) => save({ planScope: v })} placeholder="plan-…" />
          </div>
        </Panel>
        <Panel kicker="AGENT" title="안내자 모델">
          <AgentModels />
        </Panel>
      </div>
    </div>
  );
}

export function ConnectionForm({ s, save }: { s: Settings; save: (p: Partial<Settings>) => Promise<void> }) {
  return (
    <div className="grid grid-cols-2 gap-3 p-4">
      <div className="col-span-2"><Field label="MCP 주소" value={s.mcpUrl} onSave={(v) => save({ mcpUrl: v })} placeholder="http://sova-mcp.<tailnet>.ts.net:443/mcp" mono /></div>
      <Field label="AWS SSO 프로필" value={s.awsProfile} onSave={(v) => save({ awsProfile: v })} mono />
      <Field label="리전" value={s.awsRegion} onSave={(v) => save({ awsRegion: v })} mono />
      {/^https?:\/\/(127\.0\.0\.1|localhost)/.test(s.mcpUrl) && (
        <div className="col-span-2"><Field label="로컬 시험 토큰 (루프백 MCP 전용)" value={s.devToken} onSave={(v) => save({ devToken: v })} mono /></div>
      )}
      <p className="col-span-2 flex items-center gap-1.5 text-[11.5px] text-ink-soft">
        <ExternalLink size={12} /> 요청마다 SSO 세션으로 60초짜리 STS 서명 증명(audience sova-mcp)을 만들어 붙입니다. 자격 값은 저장하지 않습니다.
      </p>
    </div>
  );
}

function Field({ label, value, onSave, placeholder, mono }: { label: string; value: string; onSave: (v: string) => void; placeholder?: string; mono?: boolean }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <label className="flex flex-col gap-1">
      <span className="kicker">{label}</span>
      <input value={v} placeholder={placeholder} onChange={(e) => setV(e.target.value)} onBlur={() => v !== value && onSave(v.trim())}
        onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        className={`rounded-lg border-2 border-ink bg-[#fffaf0] px-2.5 py-1.5 outline-none focus:shadow-pop-sm ${mono ? 'font-mono text-[12px]' : ''}`} />
    </label>
  );
}

export function AgentModels() {
  const { data } = useModels();
  const auth = useQuery({ queryKey: ['agentAuth'], queryFn: () => sova.agentAuth(false) });
  const [msg, setMsg] = useState('');
  return (
    <div className="flex flex-col gap-2 p-4 text-[12.5px]">
      <div className="flex items-center gap-2 rounded-lg border-2 border-ink bg-paper px-2.5 py-1.5">
        <span className={`h-2.5 w-2.5 rounded-full border border-ink ${auth.data?.ok ? 'bg-wasabi' : 'bg-red'}`} />
        <span className="flex-1 truncate">{auth.data?.ok ? 'ChatGPT 계정으로 로그인됨' : 'ChatGPT 로그인이 필요합니다'}</span>
        <button className="btn !py-0.5 text-[11px]" onClick={async () => { setMsg('브라우저에서 로그인을 마쳐 주세요…'); const r = await sova.agentAuth(true); setMsg(r.message); await auth.refetch(); }}>
          {auth.data?.ok ? '다시 로그인' : 'ChatGPT로 로그인'}</button>
      </div>
      {msg && <p className="text-[11.5px] text-ink-soft">{msg}</p>}
      <p>앱에 내장된 OpenAI Codex 런타임(Codex SDK)을 이 기기의 <b>ChatGPT 계정</b>으로 실행합니다. API 키·플랫폼 키를 쓰지 않으며, 앱은 토큰을 읽거나 저장하지 않습니다.</p>
      <p className="text-ink-soft">기본 모델: <b className="font-mono">{data?.defaultModel ?? '—'}</b> · 사용 가능 {data?.models.length ?? 0}개. 대화마다 입력창의 모델·추론 메뉴에서 바꿉니다.</p>
      <ul className="scroll-thin max-h-40 overflow-y-auto rounded-lg border-2 border-ink/20">
        {(data?.models ?? []).map((m) => (
          <li key={m.slug} className="flex items-center gap-2 border-b border-dashed border-ink/15 px-2.5 py-1.5 last:border-0">
            <b>{m.label}</b><span className="truncate text-[11px] text-ink-soft">{m.efforts.map((e) => EFFORT_LABEL[e.id]).join(' · ')}</span>
            {m.fast && <span className="ml-auto rounded bg-mustard px-1 text-[10px] font-bold">Fast</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}
