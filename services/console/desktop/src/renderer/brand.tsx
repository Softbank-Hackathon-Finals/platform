import markImg from '../../assets/sova-mark.png';
import type { ReactNode } from 'react';
import type { Status } from '../shared/types';
import { useT } from './i18n';

/** App mark: the choko cup and soba strand from the SOVA key visual. */
export function Mark({ size = 40, dipping = false }: { size?: number; dipping?: boolean }) {
  return <img src={markImg} width={size} height={size} alt="" draggable={false} className={`shrink-0 ${dipping ? 'dip' : ''}`} />;
}

export function Wordmark({ size = 34 }: { size?: number | string }) {
  return <span className="wordmark" style={{ fontSize: size }}>SOVA</span>;
}

/** Vertical ひやしそば pill from the poster. */
export function HiyashiPill({ vertical = true }: { vertical?: boolean }) {
  return (
    <span className="inline-flex items-center justify-center rounded-full border-2 border-ink bg-ink px-1.5 py-2 font-kdisplay text-[11px] leading-[1.05] text-cream"
      style={vertical ? { writingMode: 'vertical-rl' } : undefined}>ひやしそば</span>
  );
}

export function Burst({ children, className = '', color = 'bg-mustard' }: { children: ReactNode; className?: string; color?: string }) {
  return (
    <span className={`relative inline-grid place-items-center ${className}`}>
      <span className="starburst absolute inset-0 bg-ink" />
      <span className={`starburst absolute inset-[3px] ${color}`} />
      <span className="relative px-3 text-center font-kdisplay leading-tight">{children}</span>
    </span>
  );
}

export function Stamp({ children = '冷', size = 34, title }: { children?: ReactNode; size?: number; title?: string }) {
  return <span title={title} className="stamp inline-grid shrink-0 place-items-center" style={{ width: size, height: size, fontSize: size * 0.5 }}>{children}</span>;
}

const STATUS: Record<string, { label: string; cls: string }> = {
  OK: { label: '정상 조회', cls: 'bg-cream' },
  ACCEPTED: { label: '접수', cls: 'bg-cold text-white' },
  RUNNING: { label: '진행 중', cls: 'bg-cold text-white' },
  PASSED: { label: 'PASS', cls: 'bg-wasabi' },
  FAILED: { label: 'FAIL', cls: 'bg-red text-cream' },
  BLOCKED: { label: '사람 대기', cls: 'bg-mustard' },
  CANCELLED: { label: '취소함', cls: 'bg-paper' },
  UNKNOWN: { label: '미확인', cls: 'bg-[repeating-linear-gradient(135deg,#e9dcc0_0_5px,#fff3dc_5px_10px)]' },
  NEEDS_CONFIRMATION: { label: '확인 필요', cls: 'bg-mustard' },
};
export function StatusBadge({ status }: { status: Status | string }) {
  const s = STATUS[status] ?? { label: status, cls: 'bg-cream' };
  return <span className={`inline-flex items-center whitespace-nowrap rounded-md border-2 border-ink px-1.5 py-[1px] text-[11px] font-extrabold ${s.cls}`}>{s.label}</span>;
}

export const MAIN_STAGES = [
  ['COLLECT', '수집'], ['ANALYZE', '분석'], ['PROPOSE', '제안'], ['REHEARSE', '리허설'], ['GATE', '원본 대조'],
  ['AWAIT_APPROVAL', '승인 대기'], ['SHIFT', '전환'], ['CUTOVER', '컷오버'], ['WATCH', '관찰'], ['DONE', '완료'],
] as const;
export const SIDE_STAGES: Record<string, string> = {
  FAILBACK: '복귀 중', SAFE_HOLD: '안전 정지', NEEDS_HUMAN: '사람 판단', DONE_ROLLED_BACK: '복귀 완료',
};
export const stageLabel = (s?: string) => MAIN_STAGES.find(([k]) => k === s)?.[1] ?? SIDE_STAGES[s ?? ''] ?? s ?? '—';
const HUMAN = new Set(['AWAIT_APPROVAL', 'SHIFT', 'CUTOVER']);

/** The migration as one soba strand; chopsticks mark where the plan is now. */
export function StageRail({ state }: { state?: string }) {
  const t = useT();
  const idx = MAIN_STAGES.findIndex(([k]) => k === state);
  const side = state && SIDE_STAGES[state];
  const W = 1000, step = W / (MAIN_STAGES.length - 1);
  const wave = Array.from({ length: 41 }, (_, i) => {
    const x = (i / 40) * W;
    return `${i ? 'L' : 'M'}${x.toFixed(1)} ${(30 + Math.sin(i * 0.9) * 4).toFixed(1)}`;
  }).join(' ');
  return (
    <div className="@container relative px-6 pt-12 pb-3">
      <svg viewBox={`0 0 ${W} 60`} preserveAspectRatio="none" className="absolute inset-x-6 top-12 h-[60px] w-[calc(100%-48px)]">
        <path d={wave} stroke="#1B1B1B" strokeWidth="9" fill="none" strokeLinecap="round" />
        <path d={wave} stroke="#D6C3A6" strokeWidth="5" fill="none" strokeLinecap="round" />
        {idx > 0 && <path d={wave} stroke="#7A6450" strokeWidth="5" fill="none" strokeLinecap="round"
          strokeDasharray={`${idx * step} ${W * 2}`} />}
      </svg>
      <ol className="relative grid" style={{ gridTemplateColumns: `repeat(${MAIN_STAGES.length}, minmax(0, 1fr))` }}>
        {MAIN_STAGES.map(([key, label], i) => {
          const done = idx > i || state === 'DONE';
          const now = idx === i;
          return (
            <li key={key} className="flex flex-col items-center gap-1.5">
              <span className="relative grid h-[60px] place-items-center">
                {now && (
                  <svg className="wobble absolute -top-9 left-1/2 -ml-3" width="26" height="36" viewBox="0 0 26 36" aria-hidden>
                    <path d="M4 0 L11 30 M14 0 L13 30" stroke="#1B1B1B" strokeWidth="3.4" strokeLinecap="round" />
                    <path d="M11 28 C 9 33, 15 33, 13 36" stroke="#7A6450" strokeWidth="2.6" fill="none" />
                  </svg>
                )}
                <span className={`grid place-items-center rounded-full border-[2.5px] border-ink text-[10px] font-black
                  ${now ? 'h-7 w-7 bg-mustard shadow-pop-sm' : done ? 'h-5 w-5 bg-soba text-cream' : 'h-5 w-5 bg-cream'}
                  ${HUMAN.has(key) && !done && !now ? 'ring-2 ring-red ring-offset-1 ring-offset-cream' : ''}`}>
                  {done ? '✓' : i + 1}
                </span>
              </span>
              <span className={`whitespace-nowrap text-center leading-tight ${now ? 'font-kdisplay text-[clamp(11px,1.6cqw,13px)]' : `text-[clamp(9px,1.3cqw,11px)] ${done ? 'font-bold text-ink-soft' : 'text-ink-soft'}`}`}>{t(label)}</span>
              {HUMAN.has(key) && <span className="rounded-sm bg-red px-1 text-[9px] font-black text-cream">{t('사람')}</span>}
            </li>
          );
        })}
      </ol>
      {side && (
        <div className="mt-2 flex justify-center">
          <Burst className="h-16 w-40 text-[13px]" color={state === 'SAFE_HOLD' || state === 'NEEDS_HUMAN' ? 'bg-mustard' : 'bg-cold text-white'}>{t(side)}</Burst>
        </div>
      )}
    </div>
  );
}
