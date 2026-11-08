// GitOps panels: platform Argo CD (bravo-gitops on the platform RKE2) and the customer's Argo CD (bravo-iac-gitops on
// the on-prem target). Read through each cluster's tailnet API proxy with the person's identity after a cluster-UID
// check (main/argo.ts). Datadog-style rows: health · sync · revision · last operation; problems first.
import { useQuery } from '@tanstack/react-query';
import { ExternalLink, GitBranch } from 'lucide-react';
import { ago, sova } from './lib';
import { useT } from './i18n';

const HEALTH: Record<string, string> = { Healthy: 'bg-wasabi', Progressing: 'bg-cold', Degraded: 'bg-red', Missing: 'bg-red', Suspended: 'bg-mustard', Unknown: 'bg-[#cfc6bb]' };
const rank = (a: any) => (a.health === 'Degraded' || a.health === 'Missing' ? 0 : a.sync === 'OutOfSync' ? 1 : a.health === 'Progressing' ? 2 : 3);

function ArgoCard({ side }: { side: 'platform' | 'customer' }) {
  const t = useT();
  const q = useQuery({ queryKey: ['argo', side], queryFn: () => sova.argo(side), refetchInterval: 15_000 });
  const d = q.data;
  const apps: any[] = [...(d?.apps ?? [])].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  const bad = apps.filter((a) => rank(a) < 2).length;
  return (
    <section className="panel flex min-w-0 flex-col overflow-hidden">
      <header className="flex flex-wrap items-center gap-2 border-b-2 border-ink bg-[#fffaf0] px-3 py-2">
        <GitBranch size={15} />
        <div className="min-w-0">
          <h3 className="truncate font-kdisplay text-[14px] leading-tight">{t(d?.label ?? (side === 'platform' ? '플랫폼 Argo CD' : '고객 Argo CD'))}</h3>
          <div className="truncate font-mono text-[10px] text-ink-soft">{d?.ok ? `${d.ns} · UID ${String(d.uid).slice(0, 8)} ${t('확인')}` : d ? t(d.code) : t('조회 중')}</div>
        </div>
        <span className={`ml-auto whitespace-nowrap rounded-full border-2 border-ink px-2 text-[11px] font-bold ${!d?.ok ? 'bg-[#f1ece4]' : bad ? 'bg-mustard' : 'bg-[#e9f5da]'}`}>
          {d?.ok ? `${apps.length}${t('개')} · ${bad ? `${t('주의')} ${bad}` : t('모두 정상')}` : '—'}</span>
        {d?.ui ? <button className="btn !px-1.5 !py-0 text-[10.5px]" onClick={() => window.open(d.ui, '_blank')}>UI <ExternalLink size={11} /></button>
          : <span className="whitespace-nowrap text-[10.5px] text-ink-soft" title={t('UI 노출(tailnet 중계)은 관측·Argo 노출 작업에서 진행 중')}>{t('UI 미노출')}</span>}
      </header>
      {!d?.ok && d ? <p className="halftone m-3 rounded-lg border-2 border-dashed border-ink p-2 text-[12px]">{t('Argo 상태를 읽지 못했습니다')} ({d.code}). {t('Tailscale 연결과 운영자 권한을 확인하세요.')}</p> : (
        <ul className="scroll-thin max-h-[260px] overflow-y-auto">
          {apps.map((a) => (
            <li key={a.name} className="border-b border-dashed border-ink/20 px-3 py-1.5 last:border-0" title={[a.repo, a.path, a.target, ...a.conditions].filter(Boolean).join('\n')}>
              <div className="flex min-w-0 items-center gap-2">
                <span className={`h-2.5 w-2.5 shrink-0 rounded-full border border-ink ${HEALTH[a.health] ?? HEALTH.Unknown}`} title={a.health} />
                <b className="min-w-0 truncate text-[12.5px]">{a.name}</b>
                <span className={`shrink-0 rounded border-[1.5px] border-ink px-1 text-[10px] font-black ${a.sync === 'Synced' ? 'bg-[#e9f5da]' : a.sync === 'OutOfSync' ? 'bg-mustard' : 'bg-[#f1ece4]'}`}>{a.sync}</span>
                <span className="shrink-0 text-[10px] text-ink-soft" title={a.auto ? '자동 동기화' : '사람이 동기화(Argo 자동 동기화 꺼짐): OutOfSync는 적용 대기'}>{a.auto ? 'auto' : t('수동')}</span>
                <span className="tnum ml-auto shrink-0 font-mono text-[10.5px] text-ink-soft">{String(a.revision ?? '').slice(0, 7)}</span>
              </div>
              <div className="flex min-w-0 gap-2 pl-[18px] text-[10.5px] text-ink-soft">
                <span className="min-w-0 truncate">{a.path ?? a.repo}</span>
                <span className="ml-auto shrink-0 whitespace-nowrap">{a.resources}{t('개 자원')}{a.outOfSync ? ` · OutOfSync ${a.outOfSync}` : ''}{a.unhealthy ? ` · ${t('비정상')} ${a.unhealthy}` : ''}{a.op ? ` · ${a.op.phase} ${a.op.finishedAt ? ago(a.op.finishedAt) : ''}` : ''}</span>
              </div>
              {a.conditions.length > 0 && <div className="truncate pl-[18px] text-[10.5px] font-semibold text-red">{a.conditions[0]}</div>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function GitOpsPanels() {
  return (
    <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-2">
      <ArgoCard side="platform" />
      <ArgoCard side="customer" />
    </div>
  );
}
