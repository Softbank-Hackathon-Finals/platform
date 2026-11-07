// SOVA guide panel. Structure follows the Codex desktop composer/transcript (REA of the shipped
// bundle's ko-KR messages: composer.intelligenceDropdown, permissionsDropdown, slashCommands,
// atMentionList, queuedMessage/steer, statusPlain, localConversation.workedFor/agentActivity,
// threadFindBar, userMessageNavigation, modelChanged); the look is SOVA's.
import { useQuery } from '@tanstack/react-query';
import { useAtom, useAtomValue, useSetAtom } from 'jotai';
import {
  ArrowUp, Bookmark, Check, ChevronDown, ChevronRight, ChevronUp, CornerDownLeft, Gauge, Pencil, Search, Square, Trash2,
  Wrench, X, Zap,
} from 'lucide-react';
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { EFFORT_LABEL, EFFORT_ORDER, type AgentEvent, type Effort, type PromptInfo } from '../shared/types';
import { HiyashiPill, Mark, StatusBadge, Stamp } from './brand';
import {
  activeConvAtom, blankConversation, convsAtom, findAtom, pendingConfirmAtom, planAtom, renameAtom, sova, useTool,
  type ChatItem, type Conversation,
} from './lib';
import { useLink } from './shell';
import { useT } from './i18n';

/* ---------- events → conversations ---------- */

export function useAgentEvents() {
  const setConvs = useSetAtom(convsAtom);
  const setPending = useSetAtom(pendingConfirmAtom);
  const active = useAtomValue(activeConvAtom);
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => sova.onAgent((e: AgentEvent) => {
    if (e.type === 'confirm') { setPending({ conv: e.conv, id: e.id, name: e.name, input: e.input }); return; }
    setConvs((all) => all.map((c) => (c.id === e.conv ? apply(c, e, activeRef.current) : c)));
  }), [setConvs, setPending]);
}

function apply(c: Conversation, e: AgentEvent, active: string): Conversation {
  const items = [...c.items];
  const last = items.at(-1);
  const now = Date.now();
  switch (e.type) {
    case 'text':
      if (last?.kind === 'assistant') items[items.length - 1] = { ...last, text: (last.text ?? '') + e.delta };
      else items.push({ kind: 'assistant', text: e.delta, at: now });
      return { ...c, items, thinking: false, retry: undefined, updatedAt: now };
    case 'thinking': return { ...c, thinking: e.on };
    case 'retry': return { ...c, retry: `${e.attempt}/${e.max}` };
    case 'usage': return { ...c, context: { used: e.used, total: c.context?.total } };
    case 'tool': items.push({ kind: 'tool', id: e.id, name: e.name, input: e.input, at: now }); return { ...c, items, thinking: false, updatedAt: now };
    case 'tool_result': {
      const i = items.findIndex((x) => x.kind === 'tool' && x.id === e.id);
      if (i >= 0) items[i] = { ...items[i], result: e.text, isError: e.isError };
      return { ...c, items, updatedAt: now };
    }
    case 'error': items.push({ kind: 'error', text: e.message, at: now }); return { ...c, items, busy: false, thinking: false, updatedAt: now };
    case 'done': {
      const start = items.map((x) => x.kind).lastIndexOf('user');
      const tools = items.slice(start + 1).filter((x) => x.kind === 'tool').length;
      items.push({ kind: 'turn', durationMs: e.durationMs ?? (c.turnStartedAt ? now - c.turnStartedAt : 0), tools, at: now });
      return { ...c, items, busy: false, thinking: false, retry: undefined, sessionId: e.sessionId ?? c.sessionId,
        costUsd: e.costUsd ?? c.costUsd, context: c.context && { ...c.context, total: e.total ?? c.context.total },
        unread: c.id !== active, updatedAt: now };
    }
  }
  return c;
}

/** Starts one turn: user item, then the Agent SDK run with this conversation's model/effort. */
export function useSendTurn() {
  const setConvs = useSetAtom(convsAtom);
  return (c: Conversation, text: string, display?: string) => {
    setConvs((all) => all.map((x) => x.id === c.id ? {
      ...x, busy: true, unread: false, forkPending: false, turnStartedAt: Date.now(),
      title: x.items.some((i) => i.kind === 'user') ? x.title : (display ?? text).slice(0, 40),
      items: [...x.items, { kind: 'user', text: display ?? text, at: Date.now() }], updatedAt: Date.now(),
    } : x));
    void sova.agentSend(c.id, text, { sessionId: c.sessionId, model: c.model, effort: c.effort, fast: c.fast, fork: c.forkPending });
  };
}

/** Sends the next queued prompt once a turn ends (Codex queued messages). */
export function useQueueRunner() {
  const [convs, setConvs] = useAtom(convsAtom);
  const send = useSendTurn();
  useEffect(() => {
    for (const c of convs) {
      if (c.busy || !c.queue?.length) continue;
      const [next, ...rest] = c.queue;
      setConvs((all) => all.map((x) => x.id === c.id ? { ...x, queue: rest } : x));
      send({ ...c, queue: rest }, next);
    }
  }, [convs, setConvs, send]);
}

/* ---------- transcript pieces ---------- */

function highlight(text: string, q: string): ReactNode {
  if (!q) return text;
  const parts = text.split(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi'));
  return parts.map((p, i) => (i % 2 ? <mark key={i}>{p}</mark> : <Fragment key={i}>{p}</Fragment>));
}

function Md({ text, q = '' }: { text: string; q?: string }) {
  const inline = (s: string) => s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p, i) =>
    p.startsWith('**') ? <b key={i}>{highlight(p.slice(2, -2), q)}</b>
      : p.startsWith('`') ? <code key={i} className="rounded bg-paper px-1 font-mono text-[11.5px]">{p.slice(1, -1)}</code>
        : <Fragment key={i}>{highlight(p, q)}</Fragment>);
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  let rows: string[][] = [];
  const flush = () => {
    if (list.length) { blocks.push(<ul key={blocks.length} className="ml-4 list-disc space-y-0.5">{list.map((l, i) => <li key={i}>{inline(l)}</li>)}</ul>); list = []; }
    if (rows.length) {
      const [head, ...body] = rows;
      blocks.push(
        <table key={blocks.length} className="w-full border-collapse overflow-hidden rounded-lg border-2 border-ink text-[12px]">
          <thead><tr>{head.map((c, i) => <th key={i} className="border-b-2 border-ink bg-mustard px-2 py-1 text-left">{inline(c)}</th>)}</tr></thead>
          <tbody>{body.map((r, i) => <tr key={i} className="border-b border-dashed border-ink/25 last:border-0">{r.map((c, j) => <td key={j} className="px-2 py-1 align-top">{inline(c)}</td>)}</tr>)}</tbody>
        </table>);
      rows = [];
    }
  };
  for (const line of text.split('\n')) {
    if (/^\s*\|.*\|\s*$/.test(line)) {
      if (!/^\s*\|[\s:|-]+\|\s*$/.test(line)) rows.push(line.trim().slice(1, -1).split('|').map((c) => c.trim()));
      continue;
    }
    const m = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (m) { if (rows.length) flush(); list.push(m[1]); continue; }
    flush();
    if (/^#{1,4}\s/.test(line)) blocks.push(<p key={blocks.length} className="font-kdisplay text-[14px]">{line.replace(/^#+\s/, '')}</p>);
    else if (line.trim()) blocks.push(<p key={blocks.length}>{inline(line)}</p>);
  }
  flush();
  return <div data-selectable className="space-y-1.5 text-[13px] leading-relaxed">{blocks}</div>;
}

// Present / past tense per SOVA tool (Codex agentActivity: "실행 중" → "실행했습니다").
const TOOL_VERB: Record<string, [string, string]> = {
  sova_get_context: ['연결 맥락 확인 중', '연결 맥락을 확인함'], sova_next_steps: ['다음 행동 계산 중', '다음 행동을 확인함'],
  sova_list_plans: ['계획 목록 읽는 중', '계획 목록을 읽음'], sova_get_plan: ['계획 조회 중', '계획을 조회함'],
  sova_get_progress: ['진행 이벤트 추적 중', '진행 이벤트를 추적함'], sova_compare: ['원본 대조 결과 확인 중', '원본 대조 결과를 확인함'],
  sova_get_execution: ['실행 기록 확인 중', '실행 기록을 확인함'], sova_get_observability: ['관측 지표 조회 중', '관측 지표를 조회함'],
  sova_get_security: ['보안 발견 조회 중', '보안 발견을 조회함'], sova_request_approval: ['승인 링크 준비 중', '승인 링크를 준비함'],
  sova_register_environment: ['환경 등록 요청 중', '환경 등록을 요청함'], sova_register_workload: ['앱 등록 요청 중', '앱 등록을 요청함'],
  sova_create_plan: ['계획 생성 요청 중', '계획 생성을 요청함'], ListMcpResourcesTool: ['안내 자료 찾는 중', '안내 자료를 찾음'],
  ReadMcpResourceTool: ['안내 자료 읽는 중', '안내 자료를 읽음'],
};
const verb = (name = '', done: boolean) => TOOL_VERB[name]?.[done ? 1 : 0] ?? `${name} ${done ? '호출함' : '호출 중'}`;
const pretty = (raw?: string) => { if (!raw) return '대기 중'; try { return JSON.stringify(JSON.parse(raw), null, 2); } catch { return raw; } };

function ToolRow({ items }: { items: ChatItem[] }) {
  const [open, setOpen] = useState(false);
  const item = items.at(-1)!;
  let parsed: any = null;
  try { parsed = item.result ? JSON.parse(item.result) : null; } catch { /* plain text */ }
  const running = !item.result;
  const write = /register|create_plan/.test(item.name ?? '');
  return (
    <div className="text-[12px]">
      <button onClick={() => setOpen(!open)} className="group flex w-full items-center gap-2 rounded-lg px-1.5 py-1 text-left hover:bg-paper">
        <Wrench size={12} className={write ? 'text-red' : 'text-ink-soft'} />
        <span className={running ? 'shimmer-text font-semibold' : 'font-semibold'}>{verb(item.name, !running)}</span>
        {items.length > 1 && <span className="whitespace-nowrap text-ink-soft">· {items.length}회 호출</span>}
        {parsed?.status && <StatusBadge status={parsed.status} />}
        {item.isError && <StatusBadge status="FAILED" />}
        <span className="min-w-0 flex-1 truncate text-ink-soft">{parsed?.summary ?? ''}</span>
        <ChevronRight size={13} className={`text-ink-soft opacity-0 group-hover:opacity-100 ${open ? 'rotate-90 opacity-100' : ''}`} />
      </button>
      {open && items.map((it, i) => (
        <pre key={i} data-selectable className="scroll-thin mx-1.5 mb-1.5 max-h-64 overflow-auto rounded-lg bg-ink p-2.5 font-mono text-[10.5px] text-cream">
          {`mcp__sova__${it.name}\n입력 ${JSON.stringify(it.input, null, 2)}\n\n결과 ${pretty(it.result)}`}
        </pre>
      ))}
    </div>
  );
}

/** Consecutive calls of the same tool fold into one row ("… · 3회 호출"). */
function foldTools(items: ChatItem[]) {
  const out: (ChatItem | ChatItem[])[] = [];
  for (const it of items) {
    const prev = out.at(-1);
    if (it.kind === 'tool' && Array.isArray(prev) && prev[0].name === it.name) prev.push(it);
    else out.push(it.kind === 'tool' ? [it] : it);
  }
  return out;
}

const secs = (ms = 0) => (ms < 60_000 ? `${Math.max(1, Math.round(ms / 1000))}초` : `${Math.floor(ms / 60_000)}분 ${Math.round((ms % 60_000) / 1000)}초`);

function Elapsed({ since }: { since?: number }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t); }, []);
  return <>{secs(Date.now() - (since ?? Date.now()))}</>;
}

function Note({ text }: { text: string }) {
  return <div className="mx-6 rounded-lg border-2 border-dashed border-ink/30 bg-paper px-3 py-1.5 text-center text-[11.5px] text-ink-soft">{text}</div>;
}

/** One user turn: activity folds into "N초 동안 작업함" once done; the final answer stays open. */
function Turn({ conv, user, body, index, q, bookmarked, onBookmark, last }: {
  conv: Conversation; user: ChatItem; body: ChatItem[]; index: number; q: string; bookmarked: boolean; onBookmark: () => void; last: boolean;
}) {
  const t = useT();
  const marker = body.find((x) => x.kind === 'turn');
  const done = !!marker;
  const lastText = done ? body.map((x) => x.kind).lastIndexOf('assistant') : -1;
  const activity = done ? body.filter((x, i) => x.kind !== 'turn' && x.kind !== 'error' && i !== lastText) : body;
  const [open, setOpen] = useState(false);
  return (
    <div data-turn={index} className="flex flex-col gap-2.5">
      <div className="group ml-10 flex items-start justify-end gap-1.5">
        <button aria-label="대화 차례 북마크" onClick={onBookmark}
          className={`mt-2 rounded p-0.5 ${bookmarked ? 'text-red' : 'text-ink-soft opacity-0 group-hover:opacity-100'}`}><Bookmark size={13} fill={bookmarked ? 'currentColor' : 'none'} /></button>
        <div data-selectable className="rounded-2xl rounded-br-md border-2 border-ink bg-ink px-3.5 py-2 text-[13px] text-cream shadow-[2px_2px_0_#d42a1e]">{highlight(user.text ?? '', q)}</div>
      </div>

      {done && activity.length > 0 && (
        <button onClick={() => setOpen(!open)} className="pop-in flex items-center gap-2 text-[11.5px] text-ink-soft">
          <span className="h-px flex-1 bg-ink/20" />
          <span className="flex items-center gap-1 whitespace-nowrap rounded-full border-2 border-ink/20 px-2 py-0.5 hover:border-ink hover:text-ink">
            {secs(marker!.durationMs)} {t('동안 작업함')}{marker!.tools ? ` · ${t('도구')} ${marker!.tools}` : ''}
            <ChevronDown size={12} className={open ? 'rotate-180' : ''} />
          </span>
          <span className="h-px flex-1 bg-ink/20" />
        </button>
      )}
      {(!done || open) && (
        <div className={`flex flex-col gap-1 ${done ? 'rounded-xl border-2 border-dashed border-ink/25 p-1.5' : ''}`}>
          {foldTools(activity).map((x, i) => Array.isArray(x) ? <ToolRow key={i} items={x} />
            : x.kind === 'assistant' ? <div key={i} className="flex gap-2"><span className="mt-0.5 shrink-0"><Mark size={18} /></span><Md text={x.text ?? ''} q={q} /></div>
              : x.kind === 'error' ? <div key={i} className="rounded-xl border-2 border-red bg-[#ffe3d6] px-3 py-2 text-[12px]">{x.text}</div>
                : x.kind === 'note' ? <Note key={i} text={x.text ?? ''} /> : null)}
          {!done && last && conv.busy && (
            <div className="flex items-center gap-2 px-1.5 py-1 text-[12px]">
              <span className="shimmer-text font-semibold">{conv.retry ? `↻ ${conv.retry}` : t(conv.thinking ? '생각 중' : '작업 중')}</span>
              <span className="text-ink-soft">· <Elapsed since={conv.turnStartedAt} /></span>
            </div>
          )}
        </div>
      )}
      {done && lastText >= 0 && (
        <div className="flex gap-2"><span className="mt-0.5 shrink-0"><Mark size={22} /></span><Md text={body[lastText].text ?? ''} q={q} /></div>
      )}
      {done && body.filter((x) => x.kind === 'error').map((x, i) => (
        <div key={`e${i}`} className="rounded-xl border-2 border-red bg-[#ffe3d6] px-3 py-2 text-[12px]">{x.text}</div>
      ))}
      {body.filter((x) => x.kind === 'note').length > 0 && done && body.filter((x) => x.kind === 'note').map((x, i) => <Note key={`n${i}`} text={x.text ?? ''} />)}
    </div>
  );
}

/* ---------- composer controls ---------- */

function Popover({ open, onClose, children, className = '' }: { open: boolean; onClose: () => void; children: ReactNode; className?: string }) {
  useEffect(() => {
    if (!open) return;
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="fixed inset-0 z-30" onClick={onClose} />
      <div className={`panel pop-in absolute bottom-[calc(100%+8px)] z-40 !shadow-pop-lg ${className}`}>{children}</div>
    </>
  );
}

function EffortBars({ level, n = 5 }: { level: number; n?: number }) {
  return (
    <span className="inline-flex items-end gap-[2px]" aria-hidden>
      {Array.from({ length: n }, (_, i) => i).map((i) => <span key={i} className={`w-[3px] rounded-sm ${i <= level ? 'bg-ink' : 'bg-ink/20'}`} style={{ height: 4 + i * 2 }} />)}
    </span>
  );
}

export function useModels() {
  return useQuery({ queryKey: ['models'], queryFn: () => sova.models(), staleTime: 300_000 });
}

/** Model + reasoning effort + speed in one menu (Codex composer.intelligenceDropdown). */
function IntelligencePicker({ conv, update }: { conv: Conversation; update: (p: Partial<Conversation>) => void }) {
  const [open, setOpen] = useState(false);
  const { data } = useModels();
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => sova.settings() });
  const apiMode = settings.data?.agentAuth === 'apikey';
  const models = (data?.models ?? []).filter((m) => !apiMode || m.api);
  const model = models.find((m) => m.slug === (conv.model || data?.defaultModel)) ?? models[0];
  const efforts = model?.efforts.map((e) => e.id) ?? EFFORT_ORDER.slice(0, 5);
  const lvl = Math.max(0, efforts.indexOf(conv.effort));
  const choose = (p: Partial<Conversation>) => {
    const next = models.find((m) => m.slug === p.model);
    const note = next && next.slug !== model?.slug && conv.items.length
      ? [{ kind: 'note' as const, text: `${model?.label}에서 ${next.label}(으)로 모델이 변경되었습니다. 대화 중 변경은 성능이 떨어지거나 컨텍스트가 압축될 수 있습니다.`, at: Date.now() }] : [];
    const effort = next && !next.efforts.some((e) => e.id === conv.effort) ? next.defaultEffort : p.effort ?? conv.effort;
    update({ ...p, effort, fast: next && !next.fast ? false : p.fast ?? conv.fast, items: [...conv.items, ...note] });
  };
  return (
    <div className="relative min-w-0">
      <button onClick={() => setOpen(!open)} title="추론 수준 선택 · ⌘⇧E로 전환"
        className="flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-full border-2 border-ink bg-[#fffaf0] px-2 py-0.5 text-[11.5px] font-bold hover:bg-mustard">
        {conv.fast && <Zap size={12} className="shrink-0 text-red" fill="currentColor" />}<span className="min-w-0 truncate">{model?.label ?? '모델'}</span> <EffortBars level={lvl} n={efforts.length} /> <span className="hidden @[400px]:inline">{EFFORT_LABEL[conv.effort]}</span>
        <ChevronUp size={12} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} className="left-[-110px] w-[350px] overflow-hidden">
        <div className="kicker flex items-center border-b-2 border-ink bg-mustard px-3 py-1.5 !text-ink">모델 선택<span className="ml-auto normal-case tracking-normal">{apiMode ? 'OpenAI API 키' : 'ChatGPT 계정'}</span></div>
        <div className="scroll-thin max-h-[220px] overflow-y-auto">
          {models.length === 0 && <p className="p-3 text-[12px] text-ink-soft">모델을 불러올 수 없습니다. 터미널에서 <code>codex login</code>으로 ChatGPT에 로그인하세요.</p>}
          {models.map((m) => (
            <button key={m.slug} onClick={() => choose({ model: m.slug })} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-paper">
              <span className="w-4">{m.slug === model?.slug && <Check size={14} />}</span>
              <span className="min-w-0 flex-1"><b>{m.label}</b>{m.slug === data?.defaultModel && <span className="ml-1.5 rounded bg-paper px-1 text-[10px]">기본</span>}
                <span className="block truncate text-[11px] text-ink-soft">{m.description}</span></span>
              {m.contextWindow ? <span className="font-mono text-[10px] text-ink-soft">{Math.round(m.contextWindow / 1000)}K</span> : null}
            </button>
          ))}
        </div>
        <div className="kicker border-y-2 border-ink bg-paper px-3 py-1.5">추론 수준</div>
        <div className="grid gap-1 p-2" style={{ gridTemplateColumns: `repeat(${efforts.length}, minmax(0, 1fr))` }}>
          {efforts.map((id, i) => (
            <button key={id} title={model?.efforts[i]?.note} onClick={() => choose({ effort: id })}
              className={`flex flex-col items-center gap-1 rounded-lg border-2 px-0.5 py-1.5 text-[10px] font-bold ${id === conv.effort ? 'border-ink bg-mustard shadow-pop-sm' : 'border-transparent hover:border-ink/30'}`}>
              <EffortBars level={i} n={efforts.length} />{EFFORT_LABEL[id]}
            </button>
          ))}
        </div>
        <p className="px-3 pb-2 text-[11px] text-ink-soft">{model?.efforts[lvl]?.note}{conv.effort === 'ultra' || conv.effort === 'max' ? ' · 사용 가능량을 더 빨리 소모합니다' : ''}</p>
        <label className={`flex items-center gap-2 border-t-2 border-dashed border-ink/25 px-3 py-2 ${model?.fast ? '' : 'opacity-40'}`}>
          <Zap size={14} className="text-red" />
          <span className="flex-1 text-[12px]"><b>{model?.fast?.name ?? 'Fast'}</b><span className="block text-[11px] text-ink-soft">{model?.fast?.description ?? '이 모델은 빠른 모드를 지원하지 않습니다'}</span></span>
          <input type="checkbox" disabled={!model?.fast} checked={conv.fast} onChange={(e) => choose({ fast: e.target.checked })} className="h-4 w-4 accent-[#d42a1e]" />
        </label>
      </Popover>
    </div>
  );
}

/** How SOVA actions are approved (Codex composer.permissionsDropdown → SOVA read-only / write-with-confirm). */
function ApprovalPicker() {
  const link = useLink();
  const [open, setOpen] = useState(false);
  const readOnly = link.data?.readOnly !== false;
  const set = async (ro: boolean) => { await sova.saveSettings({ readOnly: ro }); await link.refetch(); setOpen(false); };
  const options: [boolean, string, string, string][] = [
    [true, '冷', '읽기 전용', '상태·진행·관측만 읽습니다. 등록 도구가 보이지 않습니다.'],
    [false, '書', '등록 허용', '환경·앱 등록과 계획 생성을 미리보기 → 확인 창으로 엽니다.'],
  ];
  return (
    <div className="relative">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1.5 whitespace-nowrap rounded-full border-2 border-ink bg-[#fffaf0] py-0.5 pl-0.5 pr-2 text-[11px] font-bold hover:bg-mustard">
        <Stamp size={20}>{readOnly ? '冷' : '書'}</Stamp><span className="hidden @[380px]:inline">{readOnly ? '읽기 전용' : '등록 허용'}</span><ChevronUp size={12} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} className="left-0 w-[300px] overflow-hidden">
        <div className="border-b-2 border-ink bg-mustard px-3 py-2 font-kdisplay text-[13px]">SOVA 작업을 어떻게 승인할까요?</div>
        {options.map(([ro, s, t, d]) => (
          <button key={String(ro)} onClick={() => void set(ro)} className="flex w-full items-start gap-2.5 px-3 py-2.5 text-left hover:bg-paper">
            <Stamp size={26}>{s}</Stamp>
            <span className="flex-1"><b className="text-[12.5px]">{t}</b><span className="block text-[11px] text-ink-soft">{d}</span></span>
            {readOnly === ro && <Check size={14} className="mt-1" />}
          </button>
        ))}
        <p className="border-t-2 border-dashed border-ink/25 px-3 py-2 text-[11px] text-ink-soft">승인·실행·복귀는 어느 모드에서도 열리지 않습니다. 사람이 콘솔에서 합니다.</p>
      </Popover>
    </div>
  );
}

interface Suggestion { key: string; label: string; detail?: string; insert: string; run?: () => void }

function useSlash(actions: Record<string, () => void>): Suggestion[] {
  const prompts = useQuery({ queryKey: ['prompts'], queryFn: () => sova.prompts(), staleTime: 60_000 });
  const remote = (prompts.data ?? []).map((p: PromptInfo) => ({
    key: `/${p.name}`, label: `/${p.name}`, detail: p.title ?? p.description ?? 'SOVA 프롬프트', insert: `/${p.name} `,
  }));
  return [...remote,
    { key: '/new', label: '/new', detail: '새 채팅', insert: '', run: actions.new },
    { key: '/fork', label: '/fork', detail: '이 채팅을 분기 (같은 맥락, 새 세션)', insert: '', run: actions.fork },
    { key: '/rename', label: '/rename', detail: '채팅 이름 변경', insert: '', run: actions.rename },
    { key: '/copy', label: '/copy', detail: '대화를 Markdown으로 복사', insert: '', run: actions.copy },
    { key: '/clear', label: '/clear', detail: '프롬프트 지우기', insert: '', run: actions.clear },
  ];
}

function useMentions(): Suggestion[] {
  const ctx = useTool('sova_get_context', {});
  const d = ctx.data?.data;
  return [
    ...(d?.plans ?? []).map((p: any) => ({ key: p.plan_id, label: `@${p.plan_id}`, detail: `계획 · ${p.state}`, insert: `@${p.plan_id} ` })),
    ...(d?.environments ?? []).map((e: any) => ({ key: e.environment_id, label: `@${e.environment_id}`, detail: `환경 · ${e.name ?? ''}`, insert: `@${e.environment_id} ` })),
    ...(d?.workloads ?? []).map((w: any) => ({ key: w.workload_id, label: `@${w.workload_id}`, detail: `앱 · ${w.name ?? ''}`, insert: `@${w.workload_id} ` })),
  ];
}

/* ---------- panel ---------- */

const STARTERS = [
  'SOVA가 처음이에요. 무엇을 하는 플랫폼인지, 지금 우리 상태와 함께 알려 주세요.',
  '지금 계획은 어디까지 왔고, 막힌 곳이 있어?',
  '원본 대조 결과를 설명하고 승인 전에 확인할 점을 정리해 줘.',
  'demo-chat을 온프레미스 RKE2로 옮기려면 무엇부터 등록해야 해?',
];

export function AgentPanel() {
  const t = useT();
  const [convs, setConvs] = useAtom(convsAtom);
  const [active, setActive] = useAtom(activeConvAtom);
  const [find, setFind] = useAtom(findAtom);
  const setRename = useSetAtom(renameAtom);
  const plan = useAtomValue(planAtom);
  const link = useLink();
  const send = useSendTurn();
  const models = useModels();
  const [text, setText] = useState('');
  const [menuIdx, setMenuIdx] = useState(0);
  const [editingQueue, setEditingQueue] = useState<number | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const conv = convs.find((c) => c.id === active && !c.archived) ?? null;

  const update = (p: Partial<Conversation>) => conv && setConvs((all) => all.map((c) => c.id === conv.id ? { ...c, ...p, updatedAt: Date.now() } : c));
  const ensure = () => {
    if (conv) return conv;
    const c = blankConversation();
    setConvs((all) => [c, ...all]);
    setActive(c.id);
    return c;
  };

  const actions = {
    new: () => { const c = blankConversation(); setConvs((a) => [c, ...a]); setActive(c.id); },
    fork: () => {
      if (!conv) return;
      const c = blankConversation(`${conv.title} (분기)`, {
        sessionId: conv.sessionId, forkPending: !!conv.sessionId, model: conv.model, effort: conv.effort, fast: conv.fast,
        items: [...conv.items, { kind: 'note', text: '여기서 분기했습니다. 다음 질문부터 새 세션으로 이어집니다.', at: Date.now() }],
      });
      setConvs((a) => [c, ...a]);
      setActive(c.id);
    },
    rename: () => conv && setRename(conv.id),
    clear: () => setText(''),
    copy: () => conv && void navigator.clipboard.writeText(toMarkdown(conv)),
  };
  const slash = useSlash(actions);
  const mentions = useMentions();

  const slashQ = /^\/(\S*)$/.exec(text)?.[1];
  const atQ = /(?:^|\s)@(\S*)$/.exec(text)?.[1];
  const menu = slashQ !== undefined ? slash.filter((s) => s.key.slice(1).startsWith(slashQ))
    : atQ !== undefined ? mentions.filter((m) => m.key.toLowerCase().includes(atQ.toLowerCase())).slice(0, 8) : [];
  useEffect(() => setMenuIdx(0), [slashQ, atQ]);

  const pick = (s: Suggestion) => {
    if (s.run) { s.run(); setText(''); return; }
    setText(slashQ !== undefined ? s.insert : text.replace(/@(\S*)$/, s.insert));
    input.current?.focus();
  };

  const submit = async (steer = false) => {
    const msg = text.trim();
    if (!msg) return;
    const c = ensure();
    setText('');
    if (c.busy && !steer) { setConvs((all) => all.map((x) => x.id === c.id ? { ...x, queue: [...(x.queue ?? []), msg] } : x)); return; }
    if (c.busy && steer) { await sova.agentStop(c.id); await new Promise((r) => setTimeout(r, 500)); }
    // "/sova-status" etc. expand through the MCP prompt; plan arguments default to the selected plan.
    const m = /^\/(sova-[a-z-]+)\s*(.*)$/.exec(msg);
    if (m) {
      const info = (await sova.prompts()).find((p) => p.name === m[1]);
      const args: Record<string, string> = {};
      for (const a of info?.args ?? []) {
        const v = a.name === 'plan_id' ? (m[2] || plan) : a.name === 'service_name' ? (m[2] || 'demo-chat') : '';
        if (v) args[a.name] = v;
      }
      const body = await sova.prompt(m[1], args);
      if (body) { send({ ...c, busy: false }, body, msg); return; }
    }
    send({ ...c, busy: false }, msg);
  };

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.metaKey && !e.shiftKey && e.key.toLowerCase() === 'f') { e.preventDefault(); setFind((f) => ({ ...f, open: true })); }
      if (e.metaKey && e.shiftKey && e.key.toLowerCase() === 'e' && conv) {
        e.preventDefault();
        const ids = (models.data?.models.find((m) => m.slug === (conv.model || models.data?.defaultModel))?.efforts.map((x) => x.id) ?? EFFORT_ORDER) as Effort[];
        update({ effort: ids[(ids.indexOf(conv.effort) + 1) % ids.length] });
      }
      if (e.key === 'Escape' && conv?.busy && document.activeElement === input.current) void sova.agentStop(conv.id);
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  });

  const turns = useMemo(() => {
    const out: { user: ChatItem; body: ChatItem[]; index: number }[] = [];
    const head: ChatItem[] = [];
    for (const it of conv?.items ?? []) {
      if (it.kind === 'user') out.push({ user: it, body: [], index: out.length });
      else if (out.length) out.at(-1)!.body.push(it);
      else head.push(it);
    }
    return { head, out };
  }, [conv?.items]);
  const matches = find.q ? turns.out.filter((t) => [t.user, ...t.body].some((x) => (x.text ?? '').toLowerCase().includes(find.q.toLowerCase()))) : [];
  useEffect(() => {
    if (!find.q || !matches.length) return;
    scroller.current?.querySelector(`[data-turn="${matches[find.at % matches.length].index}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [find.q, find.at, matches.length]);
  useEffect(() => { if (!find.q) scroller.current?.scrollTo({ top: 1e9 }); }, [conv?.id, conv?.items.length, conv?.items.at(-1)?.text, find.q]);

  const jump = (i: number) => scroller.current?.querySelector(`[data-turn="${i}"]`)?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  const toggleBookmark = (i: number) => conv && update({ bookmarks: conv.bookmarks?.includes(i) ? conv.bookmarks.filter((b) => b !== i) : [...(conv.bookmarks ?? []), i] });
  const ctxPct = conv?.context?.total ? Math.round((conv.context.used / conv.context.total) * 100) : null;

  return (
    <aside className="@container flex w-[clamp(340px,30vw,440px)] shrink-0 flex-col border-l-2 border-ink bg-[#fffaf0]">
      <header className="drag flex h-[58px] shrink-0 items-center gap-2.5 border-b-2 border-ink bg-cream px-4">
        <Mark size={30} dipping={conv?.busy} />
        <div className="min-w-0 flex-1 leading-tight">
          <div className="flex items-center gap-1.5 font-kdisplay text-[15px]">
            {conv?.busy && <span className="inline-block h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-cold border-t-transparent" aria-label="작업 중" />}
            <span className="truncate">{conv ? conv.title : t('SOVA 안내자')}</span>
          </div>
          <div className="truncate text-[10.5px] text-ink-soft">OpenAI Codex SDK · SOVA MCP 기본 탑재{conv?.sessionId ? ' · 세션 이어짐' : ''}</div>
        </div>
        <button className="no-drag btn !px-1.5" aria-label="채팅에서 찾기 (⌘F)" onClick={() => setFind((f) => ({ ...f, open: !f.open }))}><Search size={14} /></button>
      </header>

      {find.open && (
        <div className="flex items-center gap-1.5 border-b-2 border-ink bg-mustard px-3 py-1.5">
          <Search size={13} />
          <input autoFocus value={find.q} onChange={(e) => setFind({ open: true, q: e.target.value, at: 0 })}
            onKeyDown={(e) => { if (e.key === 'Enter') setFind((f) => ({ ...f, at: f.at + (e.shiftKey ? -1 : 1) + matches.length })); if (e.key === 'Escape') setFind({ open: false, q: '', at: 0 }); }}
            placeholder={t('채팅 검색…')} className="min-w-0 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-ink/50" />
          <span className="font-mono text-[11px]">{find.q ? (matches.length ? `${(find.at % matches.length) + 1}/${matches.length}` : '0개의 결과') : ''}</span>
          <button aria-label="이전 결과" onClick={() => setFind((f) => ({ ...f, at: f.at - 1 + matches.length }))}><ChevronUp size={14} /></button>
          <button aria-label="다음 결과" onClick={() => setFind((f) => ({ ...f, at: f.at + 1 }))}><ChevronDown size={14} /></button>
          <button aria-label="찾기 닫기" onClick={() => setFind({ open: false, q: '', at: 0 })}><X size={14} /></button>
        </div>
      )}

      <div className="relative min-h-0 flex-1">
        <div ref={scroller} className="scroll-thin edge-fade h-full overflow-y-auto py-4 pl-4 pr-7">
          {!conv?.items.length ? (
            <div className="flex flex-col items-center gap-4 pt-6 text-center">
              <div className="flex items-end gap-2"><HiyashiPill /><Mark size={84} dipping /></div>
              <div>
                <div className="font-kdisplay text-[20px]">{t('무엇을 옮길까요?')}</div>
                <p className="mt-1 text-[12.5px] text-ink-soft">처음이어도 괜찮아요. 안내자가 SOVA MCP로 지금 상태를 읽고 다음 단계를 알려 줍니다.<br />
                  <b>/</b>로 명령, <b>@</b>로 계획·환경을 언급할 수 있어요. 승인과 실행은 언제나 사람이 합니다.</p>
              </div>
              <div className="flex w-full flex-col gap-2">
                {STARTERS.map((s) => <button key={s} className="btn justify-start text-left !font-semibold !whitespace-normal" onClick={() => send(ensure(), s)}>{s}</button>)}
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              {turns.head.map((x, i) => <Note key={i} text={x.text ?? ''} />)}
              {turns.out.map((t) => (
                <Turn key={t.index} conv={conv} user={t.user} body={t.body} index={t.index} q={find.q}
                  bookmarked={!!conv.bookmarks?.includes(t.index)} onBookmark={() => toggleBookmark(t.index)} last={t.index === turns.out.length - 1} />
              ))}
            </div>
          )}
        </div>
        {turns.out.length > 1 && (
          <nav aria-label="사용자 메시지" className="absolute right-1.5 top-1/2 flex -translate-y-1/2 flex-col gap-1.5">
            {turns.out.map((t) => (
              <button key={t.index} title={t.user.text} aria-label={`사용자 메시지 ${t.index + 1}(으)로 이동`} onClick={() => jump(t.index)}
                className={`h-1.5 w-3.5 rounded-full border border-ink transition-all hover:w-5 ${conv?.bookmarks?.includes(t.index) ? 'bg-red' : 'bg-cream'}`} />
            ))}
          </nav>
        )}
      </div>

      <div className="border-t-2 border-ink p-3">
        {!!conv?.queue?.length && (
          <ul className="mb-2 flex flex-col gap-1">
            {conv.queue.map((q, i) => (
              <li key={i} className="flex items-center gap-1.5 rounded-lg border-2 border-dashed border-ink/40 bg-paper px-2 py-1 text-[12px]">
                <span className="whitespace-nowrap font-mono text-[10px] text-ink-soft">대기 {i + 1}</span>
                {editingQueue === i
                  ? <input autoFocus defaultValue={q} onBlur={(e) => { update({ queue: conv.queue!.map((x, j) => j === i ? e.target.value : x) }); setEditingQueue(null); }} className="min-w-0 flex-1 bg-transparent outline-none" />
                  : <span className="min-w-0 flex-1 truncate">{q}</span>}
                <button title="현재 작업을 멈추고 지금 보내기" className="whitespace-nowrap rounded px-1 text-[11px] font-bold hover:bg-mustard"
                  onClick={async () => { update({ queue: conv.queue!.filter((_, j) => j !== i) }); await sova.agentStop(conv.id); setTimeout(() => send({ ...conv, busy: false }, q), 600); }}>
                  <CornerDownLeft size={12} className="inline" /> 지금</button>
                <button aria-label="메시지 편집" onClick={() => setEditingQueue(i)}><Pencil size={12} /></button>
                <button aria-label="대기열에 있는 메시지 삭제" onClick={() => update({ queue: conv.queue!.filter((_, j) => j !== i) })}><Trash2 size={12} /></button>
              </li>
            ))}
          </ul>
        )}
        <div className={`relative rounded-2xl border-2 border-ink bg-cream shadow-pop-sm focus-within:shadow-pop ${conv?.busy ? 'snake' : ''}`}>
          {menu.length > 0 && (
            <div className="panel pop-in absolute bottom-[calc(100%+8px)] left-0 right-0 z-40 max-h-64 overflow-y-auto p-1 !shadow-pop-lg">
              <div className="kicker px-2 py-1">{slashQ !== undefined ? '슬래시 커맨드 · SOVA 프롬프트' : '계획 · 환경 · 앱 언급'}</div>
              {menu.map((s, i) => (
                <button key={s.key} onMouseEnter={() => setMenuIdx(i)} onClick={() => pick(s)}
                  className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left ${i === menuIdx ? 'bg-mustard' : ''}`}>
                  <span className="font-mono text-[12px] font-bold">{s.label}</span><span className="truncate text-[11.5px] text-ink-soft">{s.detail}</span>
                </button>
              ))}
            </div>
          )}
          <textarea ref={input} value={text} onChange={(e) => setText(e.target.value)} rows={3}
            onKeyDown={(e) => {
              if (menu.length && ['ArrowDown', 'ArrowUp', 'Tab'].includes(e.key)) { e.preventDefault(); setMenuIdx((n) => (n + (e.key === 'ArrowUp' ? -1 : 1) + menu.length) % menu.length); return; }
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                if (menu.length) pick(menu[menuIdx]); else void submit(e.metaKey);
              }
            }}
            placeholder={t(conv?.busy ? '작업 중에 보내면 대기열에 넣습니다 · ⌘↩ 지금 조정' : 'SOVA에게 뭐든 물어봐. /로 명령, @로 계획·환경 언급')}
            className="w-full resize-none bg-transparent px-3.5 pt-3 text-[13px] outline-none" />
          <div className="flex min-w-0 items-center gap-1.5 px-2.5 pb-2.5">
            <ApprovalPicker />
            {conv ? <IntelligencePicker conv={conv} update={update} /> : null}
            <div className="flex-1" />
            {conv?.busy && text.trim()
              ? <button className="btn btn-mustard !rounded-full !px-2 !py-1 text-[11px]" title="현재 작업을 멈추고 이 지시로 조정" onClick={() => void submit(true)}>조정</button>
              : null}
            {conv?.busy
              ? <button className="btn btn-ink !rounded-full !p-1.5" aria-label="중지 (Esc)" onClick={() => void sova.agentStop(conv.id)}><Square size={14} /></button>
              : <button className="btn btn-red !rounded-full !p-1.5" aria-label="메시지 보내기" disabled={!text.trim()} onClick={() => void submit()}><ArrowUp size={16} strokeWidth={3} /></button>}
          </div>
        </div>
        <div className="mt-1.5 flex items-center gap-3 px-1 font-mono text-[10.5px] text-ink-soft">
          <span className="flex items-center gap-1"><Gauge size={11} />{t('컨텍스트')} {conv?.context ? `${Math.round(conv.context.used / 1000)}K${conv.context.total ? `/${Math.round(conv.context.total / 1000)}K · ${ctxPct}%` : ''}` : '—'}</span>
          {conv?.costUsd ? <span>${conv.costUsd.toFixed(3)}</span> : null}
          <span className="ml-auto">{t(link.data?.connected ? 'MCP 연결됨' : 'MCP 미연결')}</span>
        </div>
      </div>
    </aside>
  );
}

export function toMarkdown(c: Conversation) {
  return [`# ${c.title}`, ...c.items.map((x) => x.kind === 'user' ? `\n**사용자:** ${x.text}`
    : x.kind === 'assistant' ? `\n${x.text}` : x.kind === 'tool' ? `- \`${x.name}\`` : x.kind === 'turn' ? `\n_${secs(x.durationMs)} 동안 작업함_` : '')].join('\n');
}
