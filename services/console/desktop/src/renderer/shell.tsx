import * as Dialog from '@radix-ui/react-dialog';
import { useQuery } from '@tanstack/react-query';
import { Command } from 'cmdk';
import { useAtom, useAtomValue, useSetAtom } from 'jotai';
import {
  Activity, BookOpen, Command as Cmd, LayoutDashboard, ListTree, MessageSquarePlus, PanelRight, RefreshCw, Settings2,
  ShieldCheck,
} from 'lucide-react';
import { useEffect } from 'react';
import type { LinkStatus } from '../shared/types';
import { HiyashiPill, Mark, StatusBadge, Stamp, Wordmark, stageLabel } from './brand';
import { Threads } from './threads';
import {
  activeConvAtom, ago, blankConversation, convsAtom, panelAtom, paletteAtom, pendingConfirmAtom, planAtom, sova, useTool, viewAtom, type View,
} from './lib';

export const NAV: { id: View; label: string; icon: typeof LayoutDashboard }[] = [
  { id: 'dashboard', label: '대시보드', icon: LayoutDashboard },
  { id: 'plans', label: '이전 계획', icon: ListTree },
  { id: 'timeline', label: '진행 타임라인', icon: Activity },
  { id: 'observe', label: '관측 · 보안', icon: ShieldCheck },
  { id: 'guide', label: '처음 안내', icon: BookOpen },
  { id: 'settings', label: '연결 설정', icon: Settings2 },
];

export function useLink() {
  return useQuery<LinkStatus>({ queryKey: ['link'], queryFn: () => sova.status(), refetchInterval: 20_000 });
}

export function Sidebar() {
  const [view, setView] = useAtom(viewAtom);
  const setConvs = useSetAtom(convsAtom);
  const setActive = useSetAtom(activeConvAtom);
  const setPanel = useSetAtom(panelAtom);
  const link = useLink();
  const readOnly = link.data?.readOnly ?? true;

  return (
    <aside className="flex w-[248px] shrink-0 flex-col border-r-2 border-ink bg-cream">
      <div className="sunburst drag relative h-[132px] overflow-hidden border-b-2 border-ink">
        <div className="absolute right-3 top-3 no-drag"><HiyashiPill /></div>
        <div className="absolute bottom-3 left-4 flex items-end gap-2">
          <Mark size={46} dipping />
          <div className="flex flex-col">
            <Wordmark size={36} />
            <span className="mt-1.5 font-mono text-[9px] font-bold tracking-[.12em] text-cream [text-shadow:1px_1px_0_#1b1b1b]">MIGRATION CONSOLE</span>
          </div>
        </div>
      </div>

      <div className="p-3">
        <button className="btn btn-red w-full justify-center" onClick={() => { const c = blankConversation(); setConvs((a) => [c, ...a]); setActive(c.id); setPanel(true); }}>
          <MessageSquarePlus size={16} /> 새 채팅 <span className="ml-auto font-mono text-[10px] opacity-80">⌘N</span>
        </button>
      </div>

      <nav className="flex flex-col gap-0.5 px-2">
        {NAV.map(({ id, label, icon: Icon }) => (
          <button key={id} onClick={() => setView(id)}
            className={`flex items-center gap-2.5 rounded-lg border-2 px-2.5 py-1.5 text-left text-[13px] font-semibold
              ${view === id ? 'border-ink bg-mustard shadow-pop-sm' : 'border-transparent hover:bg-paper'}`}>
            <Icon size={16} strokeWidth={2.4} /> {label}
          </button>
        ))}
      </nav>

      <Threads />

      <div className="flex items-center gap-2.5 border-t-2 border-ink bg-paper px-3 py-2.5">
        {readOnly ? <Stamp title="읽기 전용 연결" size={30} /> : <Stamp size={30} title="쓰기 허용 연결">書</Stamp>}
        <div className="min-w-0 flex-1 leading-tight">
          <div className="flex items-center gap-1.5 text-[12px] font-bold">
            <span className={`h-2 w-2 rounded-full border border-ink ${link.data?.connected ? 'live-dot bg-wasabi' : 'bg-red'}`} />
            {link.data?.connected ? 'SOVA MCP 연결됨' : 'SOVA MCP 미연결'}
          </div>
          <div className="truncate text-[11px] text-ink-soft">{readOnly ? '읽기 전용 · 冷' : '등록·계획 생성 허용'} · 도구 {link.data?.tools.length ?? 0}개</div>
        </div>
      </div>
    </aside>
  );
}

export function TopBar() {
  const view = useAtomValue(viewAtom);
  const [plan, setPlan] = useAtom(planAtom);
  const [panel, setPanel] = useAtom(panelAtom);
  const setPalette = useSetAtom(paletteAtom);
  const ctx = useTool('sova_get_context', {}, 30_000);
  const link = useLink();
  const plans: any[] = ctx.data?.data?.plans ?? [];
  const current = plans.find((p) => p.plan_id === plan);

  useEffect(() => { if (!plan && plans[0]) setPlan(plans[0].plan_id); }, [plan, plans, setPlan]);

  return (
    <header className="drag flex h-[58px] shrink-0 items-center gap-2.5 overflow-hidden border-b-2 border-ink bg-cream px-5">
      <h1 className="whitespace-nowrap font-kdisplay text-[20px] leading-none">{NAV.find((n) => n.id === view)?.label}</h1>
      <div className="no-drag ml-3 flex items-center gap-2">
        <span className="kicker whitespace-nowrap">계획</span>
        <select value={plan} onChange={(e) => setPlan(e.target.value)}
          className="w-[190px] rounded-lg border-2 border-ink bg-[#fffaf0] px-2 py-1 font-mono text-[12px] shadow-pop-sm">
          {plans.length === 0 && <option value="">계획 없음</option>}
          {plans.map((p) => <option key={p.plan_id} value={p.plan_id}>{p.plan_id} · {stageLabel(p.state)}</option>)}
        </select>
        {current && <StatusBadge status={['DONE'].includes(current.state) ? 'PASSED' : ['SAFE_HOLD', 'NEEDS_HUMAN', 'AWAIT_APPROVAL'].includes(current.state) ? 'BLOCKED' : 'RUNNING'} />}
      </div>
      {/^https?:\/\/(127\.0\.0\.1|localhost)/.test(link.data?.url ?? '') && (
        <span className="whitespace-nowrap rounded-md border-2 border-ink bg-[repeating-linear-gradient(135deg,#ffd23f_0_6px,#fff3dc_6px_12px)] px-2 py-0.5 text-[11px] font-black">
          로컬 시험 데이터</span>
      )}
      <div className="flex-1" />
      <span className="no-drag hidden whitespace-nowrap text-[11px] text-ink-soft 2xl:inline">{ctx.data ? `조회 ${ago(ctx.data.observed_at)}` : ctx.isError ? '조회 실패' : '조회 중'}</span>
      <button className="no-drag btn !px-2" aria-label="새로 고침" onClick={() => void ctx.refetch()}><RefreshCw size={14} className={ctx.isFetching ? 'animate-spin' : ''} /></button>
      <button className="no-drag btn !px-2" onClick={() => setPalette(true)}><Cmd size={14} /><span className="font-mono text-[11px]">K</span></button>
      <button className={`no-drag btn !px-2 ${panel ? 'btn-mustard' : ''}`} aria-label="안내자 패널" onClick={() => setPanel(!panel)}><PanelRight size={15} /></button>
    </header>
  );
}

export function Palette({ ask }: { ask: (q: string) => void }) {
  const [open, setOpen] = useAtom(paletteAtom);
  const setView = useSetAtom(viewAtom);
  const setPlan = useSetAtom(planAtom);
  const ctx = useTool('sova_get_context', {});
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'k' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); setOpen((o) => !o); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setOpen]);
  const go = (fn: () => void) => { fn(); setOpen(false); };
  return (
    <Command.Dialog open={open} onOpenChange={setOpen} label="명령"
      className="panel fixed left-1/2 top-[16%] z-50 w-[560px] -translate-x-1/2 overflow-hidden !shadow-pop-lg"
      overlayClassName="fixed inset-0 z-40 bg-ink/30">
      <Command.Input autoFocus placeholder="이동하거나, 안내자에게 물어보세요…" className="w-full border-b-2 border-ink bg-transparent px-4 py-3 text-[15px] outline-none" />
      <Command.List className="scroll-thin max-h-[360px] overflow-y-auto p-2">
        <Command.Empty className="px-3 py-4 text-ink-soft">입력한 내용을 안내자에게 물으려면 Enter</Command.Empty>
        <Command.Group heading="이동" className="[&_[cmdk-group-heading]]:kicker [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
          {NAV.map((n) => <Command.Item key={n.id} onSelect={() => go(() => setView(n.id))} className="cursor-default rounded-lg px-3 py-2 data-[selected=true]:bg-mustard">{n.label}</Command.Item>)}
        </Command.Group>
        <Command.Group heading="계획" className="[&_[cmdk-group-heading]]:kicker [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
          {(ctx.data?.data?.plans ?? []).map((p: any) => (
            <Command.Item key={p.plan_id} value={`plan ${p.plan_id}`} onSelect={() => go(() => { setPlan(p.plan_id); setView('dashboard'); })}
              className="cursor-default rounded-lg px-3 py-2 font-mono text-[12px] data-[selected=true]:bg-mustard">{p.plan_id} · {stageLabel(p.state)}</Command.Item>
          ))}
        </Command.Group>
        <Command.Group heading="안내자에게" className="[&_[cmdk-group-heading]]:kicker [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
          {['SOVA가 뭐예요? 처음이에요.', '지금 계획은 어디까지 왔어?', '승인 전에 뭘 확인해야 해?'].map((q) => (
            <Command.Item key={q} onSelect={() => go(() => ask(q))} className="cursor-default rounded-lg px-3 py-2 data-[selected=true]:bg-mustard">{q}</Command.Item>
          ))}
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}

const FIELD_LABEL: Record<string, string> = {
  name: '이름', kind: '종류', roles: '역할', role: '역할', connection_ref: '연결 참조', repo_url: '저장소', app_path: '앱 경로',
  iac_path: 'IaC 경로', source_environment_id: '출발지 환경', namespace: '네임스페이스', workload_id: '앱(워크로드)',
  target_environment_id: '목적지 환경', profile_id: '평가 프로필', profile_version: '프로필 버전',
};
const TOOL_TITLE: Record<string, string> = {
  sova_register_environment: '환경을 등록할까요?', sova_register_workload: '앱을 등록할까요?', sova_create_plan: '이전 계획을 만들까요?',
};

export function ConfirmDialog() {
  const [pending, setPending] = useAtom(pendingConfirmAtom);
  const answer = (allow: boolean) => { if (pending) void sova.agentConfirm(pending.id, allow); setPending(null); };
  const fields = Object.entries(pending?.input ?? {}).filter(([k]) => k !== 'confirmation');
  return (
    <Dialog.Root open={!!pending} onOpenChange={(o) => { if (!o) answer(false); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
        <Dialog.Content onPointerDownOutside={(e) => e.preventDefault()} onInteractOutside={(e) => e.preventDefault()}
          className="panel fixed left-1/2 top-1/2 z-50 w-[520px] -translate-x-1/2 -translate-y-1/2 overflow-hidden !shadow-pop-lg">
          <div className="sunburst-soft border-b-2 border-ink px-5 pb-4 pt-5">
            <Dialog.Title className="wordmark !text-[26px]">{TOOL_TITLE[pending?.name ?? ''] ?? '실행할까요?'}</Dialog.Title>
            <Dialog.Description className="mt-2 text-[12.5px] font-semibold text-cream [text-shadow:1px_1px_0_#1b1b1b]">
              안내자가 아래 입력으로 SOVA에 등록을 요청합니다. 서버가 실제 대상 신원을 다시 확인하고 영수증을 남깁니다.
            </Dialog.Description>
          </div>
          <table className="m-5 w-[calc(100%-40px)] border-collapse text-[12.5px]">
            <tbody>
              {fields.map(([k, v]) => (
                <tr key={k} className="border-b border-dashed border-ink/30">
                  <th className="w-[130px] py-1.5 pr-3 text-left font-semibold text-ink-soft">{FIELD_LABEL[k] ?? k}</th>
                  <td data-selectable className="py-1.5 font-mono">{Array.isArray(v) ? v.join(', ') : String(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mx-5 -mt-2 rounded-lg bg-paper px-3 py-2 text-[11.5px] text-ink-soft">
            출발지 환경은 읽기만 합니다. 승인·실행·복귀는 이 확인으로 열리지 않으며, 사람이 따로 결정합니다.
          </p>
          <div className="flex justify-end gap-2 p-5">
            <button className="btn" onClick={() => answer(false)}>취소</button>
            <button className="btn btn-red" onClick={() => answer(true)}>확인하고 등록</button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
