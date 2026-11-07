import { useQueryClient } from '@tanstack/react-query';
import { useAtom, useAtomValue, useSetAtom } from 'jotai';
import { ArrowRight, LogIn, PlugZap } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { Settings } from '../shared/types';
import { AgentPanel, useAgentEvents, useQueueRunner, useSendTurn } from './agent';
import { orderedChats } from './threads';
import { Mark, Stamp, Wordmark } from './brand';
import { activeConvAtom, blankConversation, convSortAtom, convsAtom, explain, panelAtom, planAtom, sova, viewAtom } from './lib';
import { ConfirmDialog, Palette, Sidebar, TopBar, useLink } from './shell';
import { ConnectionForm, Dashboard, Guide, Observe, Plans, Timeline } from './views';
import { SettingsPage } from './settings';
import hero from '../../assets/sova-hero.jpg';

function useAsk() {
  const setConvs = useSetAtom(convsAtom);
  const setActive = useSetAtom(activeConvAtom);
  const setPanel = useSetAtom(panelAtom);
  const send = useSendTurn();
  return (q: string) => {
    const c = blankConversation(q.slice(0, 40));
    setConvs((all) => [c, ...all]);
    setActive(c.id);
    setPanel(true);
    setTimeout(() => send(c, q), 0);
  };
}

/** ⌘N new chat, ⌘1–9 jump to chat (Codex codex.command.thread1–9). */
function useShortcuts() {
  const [convs, setConvs] = useAtom(convsAtom);
  const setActive = useSetAtom(activeConvAtom);
  const setPanel = useSetAtom(panelAtom);
  const sort = useAtomValue(convSortAtom);
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (!e.metaKey || e.shiftKey) return;
      if (e.key.toLowerCase() === 'n') { e.preventDefault(); const c = blankConversation(); setConvs((a) => [c, ...a]); setActive(c.id); setPanel(true); }
      const c = /^[1-9]$/.test(e.key) ? orderedChats(convs, sort)[Number(e.key) - 1] : undefined;
      if (c) { e.preventDefault(); setActive(c.id); setPanel(true); setConvs((all) => all.map((x) => x.id === c.id ? { ...x, unread: false } : x)); }
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [convs, setConvs, setActive, setPanel, sort]);
}

function Onboarding({ done }: { done: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [msg, setMsg] = useState('');
  const link = useLink();
  const qc = useQueryClient();
  useEffect(() => { void sova.settings().then(setS); }, []);
  if (!s) return null;
  const save = async (patch: Partial<Settings>) => {
    try { setS(await sova.saveSettings(patch)); await qc.invalidateQueries(); setMsg(''); } catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div className="sunburst grid h-full grid-cols-[minmax(520px,600px)_1fr] gap-10 overflow-hidden px-12">
      <div className="drag absolute inset-x-0 top-0 h-10" />
      <div className="relative z-10 flex flex-col justify-center gap-5">
        <div className="flex items-center gap-4">
          <Mark size={64} dipping />
          <div>
            <Wordmark size={58} />
            <div className="mt-2 font-mono text-[11.5px] font-bold text-cream [text-shadow:1px_1px_0_#1b1b1b]">
              <span className="text-mustard">S</span>overeign <span className="text-mustard">O</span>pen <span className="text-mustard">V</span>endor-neutral <span className="text-mustard">A</span>rchitecture
            </div>
          </div>
        </div>
        <p className="self-start rounded-xl border-2 border-ink bg-cream px-4 py-2 font-kdisplay text-[17px] shadow-pop">멈추지 않고, 흘리지 않고, 맛은 그대로.</p>
        <div className="panel !shadow-pop-lg">
          <div className="flex items-center gap-3 border-b-2 border-ink px-5 py-3">
            <Stamp size={36} title="읽기 전용으로 시작합니다" />
            <div><div className="font-kdisplay text-[18px]">에이전트와 처음 시작하기</div>
              <div className="text-[12px] text-ink-soft">① SOVA MCP 연결 → ② 안내자에게 묻기 → ③ 대시보드에서 진행 보기</div></div>
          </div>
          <ConnectionForm s={s} save={save} />
          <div className="flex items-center gap-2 border-t-2 border-dashed border-ink/30 px-5 py-3">
            <button className="btn" onClick={async () => setMsg((await sova.ssoLogin()).message)}><LogIn size={14} /> SSO 로그인</button>
            <button className="btn" onClick={() => void link.refetch()}><PlugZap size={14} /> 연결 시험</button>
            <span className="min-w-0 flex-1 truncate text-[12px]">
              {msg || (link.data?.connected ? <b className="text-[#4c7a1c]">연결됨 · 도구 {link.data.tools.length}개</b> : link.data?.code ? explain({ code: link.data.code } as any) : '')}
            </span>
            <button className="btn btn-red" onClick={async () => { await save({ onboarded: true }); done(); }}>
              시작 <ArrowRight size={15} />
            </button>
          </div>
        </div>
        <p className="text-[11.5px] font-semibold text-cream [text-shadow:1px_1px_0_#1b1b1b]">처음엔 읽기 전용(冷)으로 연결합니다. 승인·실행은 언제나 사람이 합니다.</p>
      </div>
      <div className="relative flex items-center justify-center">
        <figure className="relative rotate-[1.5deg] rounded-2xl border-[3px] border-ink bg-ink shadow-[10px_10px_0_#1b1b1b]">
          <img src={hero} alt="SOVA 대표 이미지: ひやしそば, 止めない。こぼさない。味はそのまま。" className="block max-h-[78vh] w-full rounded-[13px] object-contain" />
        </figure>
      </div>
    </div>
  );
}

export function App() {
  const [ready, setReady] = useState<boolean | null>(null);
  const view = useAtomValue(viewAtom);
  const panel = useAtomValue(panelAtom);
  const setPlan = useSetAtom(planAtom);
  const setView = useSetAtom(viewAtom);
  const ask = useAsk();
  useAgentEvents();
  useQueueRunner();
  useShortcuts();
  const setConvs = useSetAtom(convsAtom);
  // Chats stored by an older build lack model/effort; a run cannot survive an app restart.
  useEffect(() => {
    setConvs((all) => all.map((c) => {
      const next = { ...blankConversation(c.title), ...c, id: c.id, busy: false, thinking: false, retry: undefined };
      // Claude-era sessions cannot resume on the OpenAI runtime: keep the transcript, start a new session.
      return c.model?.startsWith('claude') ? { ...next, model: '', sessionId: undefined, effort: 'medium' as const } : next;
    }));
  }, [setConvs]);
  useEffect(() => { void sova.settings().then((s) => setReady(s.onboarded)); }, []);
  useEffect(() => sova.onDeepLink((url) => {
    const plan = new URL(url).searchParams.get('plan');
    if (plan) { setPlan(plan); setView('dashboard'); }
  }), [setPlan, setView]);

  if (ready === null) return null;
  if (!ready) return <Onboarding done={() => setReady(true)} />;
  return (
    <div className="flex h-full">
      <Sidebar />
      {view === 'settings' ? <main className="min-w-0 flex-1"><SettingsPage /></main> : (
      <main className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <div className="min-h-0 flex-1">
          {view === 'dashboard' && <Dashboard ask={ask} />}
          {view === 'plans' && <Plans />}
          {view === 'timeline' && <Timeline />}
          {view === 'observe' && <Observe />}
          {view === 'guide' && <Guide ask={ask} />}
        </div>
      </main>)}
      {panel && view !== 'settings' && <AgentPanel />}
      <Palette ask={ask} />
      <ConfirmDialog />
    </div>
  );
}
