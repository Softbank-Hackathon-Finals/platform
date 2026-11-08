// Register source/target environments, the app to move, and the plan — the same SOVA MCP tools
// the guide uses (preview → confirm). The confirming call is held by the loopback link until the
// person clicks in the confirm dialog; the server re-checks the real target identity either way.
import { useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Check, Cloud, Package, Route, Server } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Stamp, StatusBadge } from './brand';
import { callTool, explain, sova, useTool } from './lib';
import { useLink } from './shell';

type Kind = 'environment' | 'workload' | 'plan';
const TOOL: Record<Kind, string> = { environment: 'sova_register_environment', workload: 'sova_register_workload', plan: 'sova_create_plan' };

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="kicker">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-ink-soft">{hint}</span>}
    </label>
  );
}
const input = 'rounded-lg border-2 border-ink bg-[#fffaf0] px-2.5 py-1.5 text-[12.5px] outline-none focus:shadow-pop-sm';

function useRegister(kind: Kind) {
  const qc = useQueryClient();
  const [state, setState] = useState<{ phase: 'idle' | 'preview' | 'done' | 'error'; text?: string; data?: any }>({ phase: 'idle' });
  const run = async (args: Record<string, unknown>) => {
    try {
      const preview = await callTool(TOOL[kind], args);
      setState({ phase: 'preview', text: preview.summary, data: preview.data });
      // Second call: held by the link until the person confirms in the dialog.
      const done = await callTool(TOOL[kind], { ...args, confirmation: preview.confirmation });
      setState({ phase: 'done', text: done.summary, data: done.data });
      await qc.invalidateQueries();
    } catch (e) {
      setState({ phase: 'error', text: explain(e) });
    }
  };
  return { state, run, reset: () => setState({ phase: 'idle' }) };
}

function Result({ s }: { s: ReturnType<typeof useRegister>['state'] }) {
  if (s.phase === 'idle') return null;
  return (
    <div className={`rounded-xl border-2 p-2.5 text-[12px] ${s.phase === 'error' ? 'border-red bg-[#ffe3d6]' : s.phase === 'done' ? 'border-ink bg-[#e9f5da]' : 'border-dashed border-ink bg-paper'}`}>
      <div className="flex items-center gap-2">
        <StatusBadge status={s.phase === 'done' ? 'ACCEPTED' : s.phase === 'error' ? 'FAILED' : 'NEEDS_CONFIRMATION'} />
        <span className="font-semibold">{s.phase === 'preview' ? '확인 창에서 입력을 확인해 주세요…' : s.text}</span>
      </div>
      {s.phase === 'done' && s.data && <pre data-selectable className="scroll-thin mt-2 max-h-32 overflow-auto rounded-lg bg-ink p-2 font-mono text-[10.5px] text-cream">{JSON.stringify(s.data, null, 2)}</pre>}
    </div>
  );
}

function Card({ icon, step, title, desc, children }: { icon: ReactNode; step: number; title: string; desc: string; children: ReactNode }) {
  return (
    <section className="panel flex flex-col">
      <header className="flex items-start gap-3 border-b-2 border-ink px-4 py-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full border-2 border-ink bg-mustard font-display text-[15px] shadow-pop-sm">{step}</span>
        <div className="min-w-0 flex-1"><div className="flex items-center gap-1.5 font-kdisplay text-[16px]">{icon}{title}</div><p className="text-[12px] text-ink-soft">{desc}</p></div>
      </header>
      <div className="flex flex-col gap-3 p-4">{children}</div>
    </section>
  );
}

function EnvironmentForm({ connections }: { connections: string[] }) {
  const r = useRegister('environment');
  const [f, setF] = useState({ name: '', kind: 'aws', role: 'source', connection_ref: connections[0] ?? '' });
  return (
    <Card icon={<Cloud size={16} />} step={1} title="출발지·목적지 환경" desc="서버에 등록된 연결 참조로만 연결합니다. 자격 값을 넣지 않습니다. 출발지는 읽기만 합니다.">
      <div className="grid grid-cols-2 gap-3">
        <Field label="이름"><input className={input} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="예: demo-chat AWS EKS" /></Field>
        <Field label="역할">
          <div className="flex overflow-hidden rounded-lg border-2 border-ink">
            {[['source', '출발지'], ['target', '목적지']].map(([v, l]) => (
              <button key={v} onClick={() => setF({ ...f, role: v })} className={`flex-1 border-r-2 border-ink py-1.5 text-[12px] font-bold last:border-r-0 ${f.role === v ? (v === 'source' ? 'bg-red text-cream' : 'bg-cold text-white') : 'bg-[#fffaf0]'}`}>{l}</button>
            ))}
          </div>
        </Field>
        <Field label="종류" hint="GCP 목적지는 고객별 확장 단계에서 열립니다">
          <div className="flex overflow-hidden rounded-lg border-2 border-ink">
            {[['aws', 'AWS'], ['onprem', '온프렘'], ['gcp', 'GCP']].map(([v, l]) => (
              <button key={v} disabled={v === 'gcp'} onClick={() => setF({ ...f, kind: v })}
                className={`flex-1 border-r-2 border-ink py-1.5 text-[12px] font-bold last:border-r-0 disabled:opacity-35 ${f.kind === v ? 'bg-mustard' : 'bg-[#fffaf0]'}`}>{l}</button>
            ))}
          </div>
        </Field>
        <Field label="연결 참조 (connection_ref)">
          {connections.length
            ? <select className={input} value={f.connection_ref} onChange={(e) => setF({ ...f, connection_ref: e.target.value })}>{connections.map((c) => <option key={c}>{c}</option>)}</select>
            : <input className={`${input} font-mono`} value={f.connection_ref} onChange={(e) => setF({ ...f, connection_ref: e.target.value })} placeholder="플랫폼 운영자가 등록한 참조" />}
        </Field>
      </div>
      <button className="btn btn-red self-end" disabled={!f.name || !f.connection_ref || r.state.phase === 'preview'}
        onClick={() => void r.run({ name: f.name, kind: f.kind, role: f.role, connection_ref: f.connection_ref })}>미리보기 후 등록 <ArrowRight size={14} /></button>
      <Result s={r.state} />
    </Card>
  );
}

function WorkloadForm({ envs, releases }: { envs: any[]; releases: any[] }) {
  const r = useRegister('workload');
  const sources = envs.filter((e) => e.roles?.includes('source'));
  const [f, setF] = useState({ name: '', repo_url: releases[0]?.repo_url ?? '', app_path: releases[0]?.app_path ?? '', iac_path: releases[0]?.iac_path ?? '',
    source_environment_id: sources[0]?.environment_id ?? '', namespace: sources[0]?.namespace ?? '' });
  const set = (k: string) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <Card icon={<Package size={16} />} step={2} title="옮길 앱" desc="저장소·앱 경로·IaC 경로가 출발지에 실제로 배포된 것과 같은지 서버가 확인합니다.">
      <div className="grid grid-cols-2 gap-3">
        <Field label="이름"><input className={input} value={f.name} onChange={set('name')} placeholder="예: demo-chat" /></Field>
        <Field label="출발지 환경">
          <select className={input} value={f.source_environment_id} onChange={set('source_environment_id')}>
            {sources.length === 0 && <option value="">먼저 출발지를 등록하세요</option>}
            {sources.map((e) => <option key={e.environment_id} value={e.environment_id}>{e.name ?? e.environment_id}</option>)}
          </select>
        </Field>
        {releases.length > 0 && (
          <div className="col-span-2"><Field label="운영자가 승인한 저장소">
            <select className={`${input} font-mono`} value={releases.findIndex((x) => x.repo_url === f.repo_url && x.app_path === f.app_path && x.iac_path === f.iac_path)}
              onChange={(e) => { const x = releases[Number(e.target.value)]; if (x) setF({ ...f, repo_url: x.repo_url, app_path: x.app_path, iac_path: x.iac_path, source_environment_id: x.environment_id }); }}>
              <option value={-1}>직접 입력</option>
              {releases.map((x, i) => <option key={i} value={i}>{x.repo_url.replace(/^https:\/\/github\.com\//, '')} · {x.app_path} · {x.environment_id}</option>)}
            </select>
          </Field></div>
        )}
        <div className="col-span-2"><Field label="저장소 (GitHub URL)"><input className={`${input} font-mono`} value={f.repo_url} onChange={set('repo_url')} placeholder="https://github.com/org/app" /></Field></div>
        <Field label="앱 경로"><input className={`${input} font-mono`} value={f.app_path} onChange={set('app_path')} placeholder="app" /></Field>
        <Field label="IaC 경로"><input className={`${input} font-mono`} value={f.iac_path} onChange={set('iac_path')} placeholder="deploy/" /></Field>
        <Field label="네임스페이스"><input className={`${input} font-mono`} value={f.namespace} onChange={set('namespace')} placeholder="app-src" /></Field>
      </div>
      <button className="btn btn-red self-end" disabled={!f.name || !f.repo_url || !f.source_environment_id || r.state.phase === 'preview'} onClick={() => void r.run(f)}>미리보기 후 등록 <ArrowRight size={14} /></button>
      <Result s={r.state} />
    </Card>
  );
}

// Same name registered several times: newest first, and every option says which registration it is.
const newest = (rows: any[]) => [...rows].sort((x, y) => String(y.registered_at).localeCompare(String(x.registered_at)));
const when = (iso?: string) => (iso ? new Date(iso).toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');

function PlanForm({ envs, workloads, profiles, lastPlan }: { envs: any[]; workloads: any[]; profiles: any[]; lastPlan?: string }) {
  const r = useRegister('plan');
  const apps = newest(workloads);
  const targets = newest(envs.filter((e) => e.roles?.includes('target')));
  // Default to what the latest plan used (the inputs the platform last ran), else the newest registrations.
  const last = useTool('sova_get_plan', lastPlan ? { plan_id: lastPlan } : null).data?.data;
  const pick = (rows: any[], key: string, want?: string) => rows.find((x) => x[key] === want)?.[key] ?? rows[0]?.[key] ?? '';
  const defaults = {
    workload_id: pick(apps, 'workload_id', last?.workload_id),
    target_environment_id: pick(targets, 'environment_id', last?.target_environment_id),
    profile: (() => { const p = profiles.find((x) => x.profile_id === last?.profile_id && String(x.profile_version) === String(last?.profile_version)) ?? profiles[0]; return p ? `${p.profile_id}@${p.profile_version}` : ''; })(),
  };
  const [edit, setF] = useState<Partial<typeof defaults>>({});
  const f = { ...defaults, ...edit };
  const [pid, pver] = f.profile.split('@');
  return (
    <Card icon={<Route size={16} />} step={3} title="이전 계획" desc="앱과 목적지를 고르면 계획을 만듭니다. 같은 입력은 같은 계획 digest가 됩니다. 이후 단계는 워크플로 호스트가 진행합니다.">
      <div className="grid grid-cols-1 gap-3 @3xl:grid-cols-3">
        <Field label="앱"><select className={input} value={f.workload_id} onChange={(e) => setF({ ...edit, workload_id: e.target.value })}>
          {apps.length === 0 && <option value="">먼저 앱을 등록하세요</option>}
          {apps.map((w) => <option key={w.workload_id} value={w.workload_id}>{`${w.name ?? ''} · ${w.workload_id.slice(9, 17)} · ${w.source_environment_id} · ${when(w.registered_at)}`}</option>)}</select></Field>
        <Field label="목적지 환경"><select className={input} value={f.target_environment_id} onChange={(e) => setF({ ...edit, target_environment_id: e.target.value })}>
          {targets.length === 0 && <option value="">먼저 목적지를 등록하세요</option>}
          {targets.map((e) => <option key={e.environment_id} value={e.environment_id}>{`${e.name ?? ''} · ${e.environment_id} · ${when(e.registered_at)}`}</option>)}</select></Field>
        <Field label="평가 프로필"><select className={input} value={f.profile} onChange={(e) => setF({ ...edit, profile: e.target.value })}>
          {profiles.map((p) => <option key={`${p.profile_id}@${p.profile_version}`}>{`${p.profile_id}@${p.profile_version}`}</option>)}</select></Field>
      </div>
      <button className="btn btn-red self-end" disabled={!f.workload_id || !f.target_environment_id || !pid || r.state.phase === 'preview'}
        onClick={() => void r.run({ workload_id: f.workload_id, target_environment_id: f.target_environment_id, profile_id: pid, profile_version: pver })}>미리보기 후 계획 생성 <ArrowRight size={14} /></button>
      <Result s={r.state} />
    </Card>
  );
}

export function Register() {
  const link = useLink();
  const ctx = useTool('sova_get_context', {}, 30_000);
  const d = ctx.data?.data ?? {};
  const readOnly = link.data?.readOnly !== false;
  return (
    <div className="scroll-thin @container h-full overflow-y-auto p-5">
      <div className="mb-4 flex items-center gap-3 rounded-2xl border-2 border-ink bg-paper px-4 py-3">
        <Server size={18} />
        <p className="flex-1 text-[12.5px]">출발지·목적지 → 앱 → 계획 순서로 등록합니다. 각 등록은 <b>미리보기 → 확인 창</b>을 거치고, 서버가 실제 대상의 신원을 읽어 영수증을 남깁니다.
          승인·실행은 여기서 열리지 않습니다.</p>
        {readOnly ? (
          <button className="btn btn-mustard" onClick={async () => { await sova.saveSettings({ readOnly: false }); await link.refetch(); }}><Stamp size={18}>書</Stamp> 등록 허용으로 전환</button>
        ) : <span className="flex items-center gap-1.5 text-[12px] font-bold"><Check size={14} /> 등록 허용 연결</span>}
      </div>
      {ctx.isError && <p className="mb-4 rounded-lg border-2 border-dashed border-ink p-3 text-[12.5px]">{explain(ctx.error)}</p>}
      <div className={`grid gap-4 ${readOnly ? 'pointer-events-none opacity-50' : ''}`}>
        <EnvironmentForm connections={d.connections ?? []} />
        <WorkloadForm envs={d.environments ?? []} releases={d.releases ?? []} />
        <PlanForm envs={d.environments ?? []} workloads={d.workloads ?? []} profiles={d.profiles ?? []} lastPlan={d.plans?.[0]?.plan_id} />
      </div>
    </div>
  );
}
