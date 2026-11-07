// Chat (session) management in the sidebar, after Codex sidebarElectron: pinned + recent sections,
// sort (updated/created), search, unread and running marks, ⌘1–9 badges, row menu (rename, pin,
// mark unread, fork, copy as Markdown, archive, delete with confirm) and an archive view.
import { useAtom, useAtomValue, useSetAtom } from 'jotai';
import { Archive, ArrowLeft, CircleDot, Copy, GitFork, MoreHorizontal, Pencil, Pin, Search, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { toMarkdown } from './agent';
import { useT } from './i18n';
import {
  activeConvAtom, ago, archiveViewAtom, blankConversation, convSortAtom, convsAtom, panelAtom, renameAtom, sova,
  type Conversation,
} from './lib';

export function orderedChats(convs: Conversation[], sort: 'updated' | 'created') {
  const key = sort === 'updated' ? 'updatedAt' : 'createdAt';
  const live = convs.filter((c) => !c.archived).sort((a, b) => b[key] - a[key]);
  return [...live.filter((c) => c.pinned), ...live.filter((c) => !c.pinned)];
}

function ThreadMenu({ c, onClose }: { c: Conversation; onClose: () => void }) {
  const t = useT();
  const setConvs = useSetAtom(convsAtom);
  const setActive = useSetAtom(activeConvAtom);
  const setRename = useSetAtom(renameAtom);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const patch = (p: Partial<Conversation>) => { setConvs((all) => all.map((x) => x.id === c.id ? { ...x, ...p } : x)); onClose(); };
  const fork = () => {
    const f = blankConversation(`${c.title} (분기)`, {
      sessionId: c.sessionId, forkPending: !!c.sessionId, model: c.model, effort: c.effort, fast: c.fast,
      items: [...c.items, { kind: 'note', text: '여기서 분기했습니다. 다음 질문부터 새 세션으로 이어집니다.', at: Date.now() }],
    });
    setConvs((all) => [f, ...all]); setActive(f.id); onClose();
  };
  const archive = async () => { if (c.busy) await sova.agentStop(c.id); patch({ archived: true, pinned: false }); };
  const row = 'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] hover:bg-mustard';
  return (
    <>
      <div className="fixed inset-0 z-30" onClick={onClose} />
      <div className="panel pop-in absolute right-1 top-8 z-40 w-[212px] p-1 !shadow-pop-lg">
        {confirmDelete ? (
          <div className="p-2 text-[12px]">
            <b className="font-kdisplay text-[14px]">{t('채팅을 삭제할까요?')}</b>
            <p className="mt-1 text-ink-soft">이 기기에서 이 채팅을 영구적으로 삭제합니다. 되돌릴 수 없습니다.</p>
            <div className="mt-2 flex justify-end gap-1.5">
              <button className="btn !py-0.5 text-[11px]" onClick={() => setConfirmDelete(false)}>취소</button>
              <button className="btn btn-red !py-0.5 text-[11px]" onClick={() => { setConvs((all) => all.filter((x) => x.id !== c.id)); onClose(); }}>삭제</button>
            </div>
          </div>
        ) : (
          <>
            <button className={row} onClick={() => { setRename(c.id); onClose(); }}><Pencil size={13} /> {t('이름 변경')}</button>
            <button className={row} onClick={() => patch({ pinned: !c.pinned })}><Pin size={13} /> {t(c.pinned ? '고정 해제' : '고정')}</button>
            <button className={row} onClick={() => patch({ unread: true })}><CircleDot size={13} /> {t('읽지 않음으로 표시')}</button>
            <button className={row} onClick={fork}><GitFork size={13} /> {t('채팅 분기')}</button>
            <button className={row} onClick={() => { void navigator.clipboard.writeText(toMarkdown(c)); onClose(); }}><Copy size={13} /> {t('Markdown으로 복사')}</button>
            <div className="my-1 border-t-2 border-dashed border-ink/20" />
            <button className={row} onClick={() => void archive()}><Archive size={13} /> {t(c.busy ? '중지하고 보관' : '보관')}</button>
            <button className={`${row} text-red`} onClick={() => setConfirmDelete(true)}><Trash2 size={13} /> {t('삭제')}</button>
          </>
        )}
      </div>
    </>
  );
}

function ThreadRow({ c, active, n }: { c: Conversation; active: boolean; n?: number }) {
  const setConvs = useSetAtom(convsAtom);
  const setActive = useSetAtom(activeConvAtom);
  const setPanel = useSetAtom(panelAtom);
  const [renaming, setRenaming] = useAtom(renameAtom);
  const [menu, setMenu] = useState(false);
  const open = () => { setActive(c.id); setPanel(true); setConvs((all) => all.map((x) => x.id === c.id ? { ...x, unread: false } : x)); };
  return (
    <div className={`group relative flex items-center rounded-lg border-2 ${active ? 'border-ink bg-paper' : 'border-transparent hover:bg-paper'}`}
      onContextMenu={(e) => { e.preventDefault(); setMenu(true); }}>
      {c.busy && <span className="ml-2 inline-block h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-cold border-t-transparent" aria-label="작업 중" />}
      {renaming === c.id ? (
        <input autoFocus defaultValue={c.title} className="min-w-0 flex-1 rounded border-2 border-ink bg-cream px-1.5 py-1 text-[12.5px] outline-none"
          onBlur={(e) => { const t = e.target.value.trim(); if (t) setConvs((all) => all.map((x) => x.id === c.id ? { ...x, title: t } : x)); setRenaming(null); }}
          onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') setRenaming(null); }} />
      ) : (
        <button className="min-w-0 flex-1 truncate px-2 py-1.5 text-left text-[12.5px]" onClick={open} onDoubleClick={() => setRenaming(c.id)}>
          <span className={c.unread ? 'font-extrabold' : ''}>{c.title}</span>
        </button>
      )}
      {c.unread && <span className="mr-1 h-2 w-2 shrink-0 rounded-full border border-ink bg-red" aria-label="읽지 않음" />}
      <span className="mr-1.5 shrink-0 font-mono text-[10px] text-ink-soft group-hover:hidden">
        {n ? `⌘${n}` : ago(new Date(c.updatedAt).toISOString()).replace(' 전', '')}</span>
      <button aria-label="채팅 액션" className="mr-1 hidden rounded p-0.5 hover:bg-cream group-hover:block" onClick={() => setMenu(true)}><MoreHorizontal size={14} /></button>
      {menu && <ThreadMenu c={c} onClose={() => setMenu(false)} />}
    </div>
  );
}

export function Threads() {
  const t = useT();
  const [convs, setConvs] = useAtom(convsAtom);
  const active = useAtomValue(activeConvAtom);
  const [sort, setSort] = useAtom(convSortAtom);
  const [archive, setArchive] = useAtom(archiveViewAtom);
  const [q, setQ] = useState('');
  const match = (c: Conversation) => !q || c.title.toLowerCase().includes(q.toLowerCase());
  const ordered = orderedChats(convs, sort);
  const shown = ordered.filter(match);
  const pinned = shown.filter((c) => c.pinned);
  const rest = shown.filter((c) => !c.pinned);
  const archived = convs.filter((c) => c.archived && match(c));
  const num = (c: Conversation) => { const i = ordered.indexOf(c); return i >= 0 && i < 9 ? i + 1 : undefined; };
  return (
    <>
      <div className="mt-4 flex items-center gap-1 px-4">
        <span className="kicker flex-1">{t(archive ? '보관함' : '채팅')}</span>
        {!archive && <button className="kicker rounded px-1 hover:bg-paper" title="사이드바 정렬" onClick={() => setSort(sort === 'updated' ? 'created' : 'updated')}>
          {t(sort === 'updated' ? '최근 업데이트' : '생성일')} ↕</button>}
      </div>
      <div className="mx-3 mt-1 flex items-center gap-1.5 rounded-lg border-2 border-ink/20 bg-[#fffaf0] px-2 focus-within:border-ink">
        <Search size={12} className="text-ink-soft" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t(archive ? '보관된 채팅 검색' : '채팅 검색')} className="min-w-0 flex-1 bg-transparent py-1 text-[12px] outline-none" />
      </div>
      <div className="scroll-thin mt-1 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {archive ? (
          archived.length === 0 ? <p className="px-2 py-3 text-[12px] text-ink-soft">{q ? '일치하는 보관된 채팅이 없습니다' : '보관된 채팅 없음'}</p> : archived.map((c) => (
            <div key={c.id} className="flex items-center gap-1 rounded-lg px-2 py-1.5 hover:bg-paper">
              <span className="min-w-0 flex-1 truncate text-[12.5px]">{c.title}</span>
              <button className="rounded px-1 text-[11px] font-bold hover:bg-mustard" onClick={() => setConvs((all) => all.map((x) => x.id === c.id ? { ...x, archived: false } : x))}>복원</button>
              <button aria-label="삭제" className="rounded p-0.5 text-red hover:bg-cream" onClick={() => setConvs((all) => all.filter((x) => x.id !== c.id))}><Trash2 size={12} /></button>
            </div>
          ))
        ) : (
          <>
            {shown.length === 0 && <p className="px-2 py-3 text-[12px] text-ink-soft">{q ? '일치하는 채팅이 없습니다.' : '안내자에게 묻고 싶은 것을 새 채팅으로 시작하세요.'}</p>}
            {pinned.length > 0 && <div className="kicker px-2 pb-0.5 pt-1">{t('고정됨')}</div>}
            {pinned.map((c) => <ThreadRow key={c.id} c={c} active={c.id === active} n={num(c)} />)}
            {pinned.length > 0 && rest.length > 0 && <div className="kicker px-2 pb-0.5 pt-2">{t('최근')}</div>}
            {rest.map((c) => <ThreadRow key={c.id} c={c} active={c.id === active} n={num(c)} />)}
          </>
        )}
      </div>
      <button onClick={() => setArchive(!archive)} className="mx-3 mb-2 flex items-center gap-1.5 rounded-lg px-2 py-1 text-[11.5px] text-ink-soft hover:bg-paper">
        {archive ? <><ArrowLeft size={12} /> {t('사이드바로 돌아가기')}</> : <><Archive size={12} /> {t('보관함')} {convs.filter((c) => c.archived).length || ''}</>}
      </button>
    </>
  );
}
