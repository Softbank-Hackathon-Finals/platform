// Full-page settings with a section rail, after Codex (settings.nav: back to app, 개인/통합 headings,
// 일반 › 언어·동작 줄이기, 연결, 키보드 단축키, 보관된 채팅).
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAtom, useSetAtom } from 'jotai';
import { ArrowLeft, Cloud, ExternalLink, Globe2, Keyboard, MessageSquare, PlugZap, Sparkles } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import type { Settings } from '../shared/types';
import { Stamp } from './brand';
import { LANGS, langAtom, useT } from './i18n';
import { archiveViewAtom, sova, viewAtom } from './lib';
import { AgentModels, SettingsView } from './views';

type Section = 'general' | 'connection' | 'guide' | 'clouds' | 'keys';

/** Console deep links for the person's own accounts. */
export function useConsoleLinks() {
  const aws = useQuery({ queryKey: ['awsProfile'], queryFn: () => sova.awsProfile() });
  const s = useQuery({ queryKey: ['settings'], queryFn: () => sova.settings() });
  const a = aws.data ?? {};
  const region = a.region ?? s.data?.awsRegion ?? 'ap-northeast-2';
  const awsDest = (path: string) => `https://${region}.console.aws.amazon.com/${path}`;
  const awsUrl = (path: string) => a.startUrl && a.accountId && a.roleName
    ? `${a.startUrl.replace(/\/$/, '')}/#/console?account_id=${a.accountId}&role_name=${encodeURIComponent(a.roleName)}&destination=${encodeURIComponent(awsDest(path))}`
    : null;
  const gcp = s.data?.gcpProject;
  const tailnet = s.data?.tailnet;
  return {
    aws: { ready: !!awsUrl(''), account: a.accountId, role: a.roleName, region,
      eks: awsUrl(`eks/home?region=${region}#/clusters`), ec2: awsUrl(`ec2/home?region=${region}#Instances:`),
      cloudwatch: awsUrl(`cloudwatch/home?region=${region}`), home: awsUrl(`console/home?region=${region}`) },
    gcp: { ready: !!gcp, project: gcp, gke: gcp ? `https://console.cloud.google.com/kubernetes/list/overview?project=${gcp}` : null,
      home: gcp ? `https://console.cloud.google.com/home/dashboard?project=${gcp}` : null },
    tailscale: { ready: true, tailnet, machines: `https://login.tailscale.com/admin/machines${tailnet ? `?tailnet=${encodeURIComponent(tailnet)}` : ''}` },
    forKind: (kind: string) => kind === 'aws' ? awsUrl(`eks/home?region=${region}#/clusters`) : kind === 'gcp' && gcp
      ? `https://console.cloud.google.com/kubernetes/list/overview?project=${gcp}` : `https://login.tailscale.com/admin/machines`,
  };
}

const open = (url?: string | null) => { if (url) window.open(url, '_blank'); };

function Row({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-4 border-b-2 border-dashed border-ink/15 px-5 py-4 last:border-0">
      <div className="min-w-0 flex-1"><div className="font-bold">{title}</div>{desc && <div className="mt-0.5 text-[12px] text-ink-soft">{desc}</div>}</div>
      {children}
    </div>
  );
}

function CloudCard({ logo, name, who, ready, links, children }: { logo: ReactNode; name: string; who: ReactNode; ready: boolean; links: [string, string | null][]; children?: ReactNode }) {
  const t = useT();
  return (
    <div className="panel flex flex-col gap-3 p-4">
      <div className="flex items-center gap-3">
        <span className="grid h-10 w-10 place-items-center rounded-xl border-2 border-ink bg-cream font-display text-[13px]">{logo}</span>
        <div className="min-w-0 flex-1"><div className="font-kdisplay text-[16px]">{name}</div><div className="truncate text-[11.5px] text-ink-soft">{who}</div></div>
        <span className={`rounded border-2 border-ink px-1.5 text-[10px] font-black ${ready ? 'bg-wasabi' : 'bg-mustard'}`}>{t(ready ? '연결됨' : '설정 필요')}</span>
      </div>
      {children}
      <div className="flex flex-wrap gap-1.5">
        {links.map(([label, url]) => (
          <button key={label} disabled={!url} onClick={() => open(url)} className="btn !py-0.5 text-[11.5px]"><ExternalLink size={12} /> {t(label)}</button>
        ))}
      </div>
    </div>
  );
}

function Clouds() {
  const t = useT();
  const links = useConsoleLinks();
  const qc = useQueryClient();
  const [s, setS] = useState<Settings | null>(null);
  useEffect(() => { void sova.settings().then(setS); }, []);
  const save = async (patch: Partial<Settings>) => { setS(await sova.saveSettings(patch)); await qc.invalidateQueries({ queryKey: ['settings'] }); };
  return (
    <div className="grid grid-cols-1 gap-4 p-5 xl:grid-cols-3">
      <CloudCard logo="AWS" name="Amazon Web Services" ready={links.aws.ready}
        who={links.aws.ready ? `${links.aws.role} · ${links.aws.account} · ${links.aws.region}` : `AWS SSO 프로필 "${s?.awsProfile ?? ''}"에 포털 주소·계정·역할이 없습니다`}
        links={[['EKS 클러스터', links.aws.eks], ['EC2', links.aws.ec2], ['CloudWatch', links.aws.cloudwatch], ['콘솔 홈', links.aws.home]]}>
        <p className="text-[11.5px] text-ink-soft">{t('IAM Identity Center 포털을 거쳐 이 역할로 콘솔을 엽니다. 브라우저의 SSO 세션을 씁니다.')}</p>
      </CloudCard>
      <CloudCard logo="GCP" name="Google Cloud" ready={links.gcp.ready}
        who={links.gcp.ready ? `${t('프로젝트 ID')} ${links.gcp.project}` : t('프로젝트 ID를 넣으면 Google 계정으로 콘솔을 엽니다')}
        links={[['GKE 클러스터', links.gcp.gke], ['콘솔 홈', links.gcp.home]]}>
        <input defaultValue={s?.gcpProject} placeholder={t('프로젝트 ID')} onBlur={(e) => void save({ gcpProject: e.target.value.trim() })}
          className="rounded-lg border-2 border-ink bg-[#fffaf0] px-2.5 py-1.5 font-mono text-[12px] outline-none" />
      </CloudCard>
      <CloudCard logo="TS" name={t('온프렘 · Tailscale')} ready={links.tailscale.ready}
        who={links.tailscale.tailnet ? `tailnet ${links.tailscale.tailnet}` : t('온프렘 목적지는 Tailscale 고정 이름으로 들어갑니다')}
        links={[['Tailscale 기기', links.tailscale.machines]]}>
        <input defaultValue={s?.tailnet} placeholder={t('tailnet 이름 (선택)')} onBlur={(e) => void save({ tailnet: e.target.value.trim() })}
          className="rounded-lg border-2 border-ink bg-[#fffaf0] px-2.5 py-1.5 font-mono text-[12px] outline-none" />
      </CloudCard>
    </div>
  );
}

const KEYS: [string, string][] = [
  ['⌘N', '새 채팅'], ['⌘1 – ⌘9', '채팅으로 이동'], ['⌘K', '명령 팔레트'], ['⌘F', '채팅에서 찾기'], ['⌘⇧E', '추론 수준 전환'],
  ['Enter / ⇧Enter', '보내기 / 줄바꿈'], ['⌘Enter', '작업 중 지금 조정'], ['Esc', '안내자 중지'], ['/', 'SOVA 프롬프트'], ['@', '계획·환경 언급'],
];

export function SettingsPage({ initial = 'general' as Section }) {
  const t = useT();
  const [section, setSection] = useState<Section>(initial);
  const [lang, setLang] = useAtom(langAtom);
  const setView = useSetAtom(viewAtom);
  const setArchive = useSetAtom(archiveViewAtom);
  const [reduced, setReduced] = useState(() => document.documentElement.dataset.reducedMotion === 'true');
  useEffect(() => { document.documentElement.dataset.reducedMotion = String(reduced); try { localStorage.setItem('sova.reducedMotion', String(reduced)); } catch { /* ok */ } }, [reduced]);
  const nav: { heading: string; items: [Section, string, ReactNode][] }[] = [
    { heading: '개인', items: [['general', '일반', <Globe2 key="g" size={15} />], ['keys', '키보드 단축키', <Keyboard key="k" size={15} />]] },
    { heading: '통합', items: [['connection', '연결', <PlugZap key="c" size={15} />], ['guide', '안내자', <Sparkles key="s" size={15} />], ['clouds', '클라우드 계정', <Cloud key="cl" size={15} />]] },
  ];
  return (
    <div className="flex h-full">
      <nav className="w-[220px] shrink-0 border-r-2 border-ink bg-paper p-3">
        <button className="mb-3 flex items-center gap-1.5 text-[12.5px] font-bold hover:underline" onClick={() => setView('dashboard')}><ArrowLeft size={14} /> {t('앱으로 돌아가기')}</button>
        {nav.map((g) => (
          <div key={g.heading} className="mb-3">
            <div className="kicker px-2 pb-1">{t(g.heading)}</div>
            {g.items.map(([id, label, icon]) => (
              <button key={id} onClick={() => setSection(id)} className={`flex w-full items-center gap-2 rounded-lg border-2 px-2.5 py-1.5 text-left text-[13px] font-semibold ${section === id ? 'border-ink bg-mustard shadow-pop-sm' : 'border-transparent hover:bg-cream'}`}>
                {icon} <span className="min-w-0 truncate" title={t(label)}>{t(label)}</span>
              </button>
            ))}
          </div>
        ))}
        <button className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] font-semibold hover:bg-cream" onClick={() => { setArchive(true); setView('dashboard'); }}>
          <MessageSquare size={15} /> {t('보관된 채팅')}</button>
      </nav>
      <div className="scroll-thin min-w-0 flex-1 overflow-y-auto">
        <div className="sunburst-soft border-b-2 border-ink px-6 py-5"><div className="wordmark text-[30px]">{t({ general: '일반', keys: '키보드 단축키', connection: '연결', guide: '안내자', clouds: '클라우드 계정' }[section])}</div></div>
        {section === 'general' && (
          <div className="m-5 panel">
            <Row title={t('화면 언어')} desc={t('메뉴·버튼·안내 문구의 언어입니다. 안내자는 질문한 언어로 답합니다.')}>
              <div className="flex gap-1.5">
                {LANGS.map((l) => (
                  <button key={l.id} onClick={() => setLang(l.id)} className={`rounded-lg border-2 px-3 py-1.5 text-[13px] font-bold ${lang === l.id ? 'border-ink bg-mustard shadow-pop-sm' : 'border-ink/25 hover:border-ink'}`}>{l.label}</button>
                ))}
              </div>
            </Row>
            <Row title={t('동작 줄이기')} desc={t('애니메이션을 줄입니다.')}>
              <input type="checkbox" checked={reduced} onChange={(e) => setReduced(e.target.checked)} className="h-5 w-5 accent-[#d42a1e]" />
            </Row>
          </div>
        )}
        {section === 'connection' && <SettingsView />}
        {section === 'guide' && <div className="m-5 panel"><AgentModels /></div>}
        {section === 'clouds' && <Clouds />}
        {section === 'keys' && (
          <div className="m-5 panel">
            {KEYS.map(([k, v]) => <Row key={k} title={v}><kbd className="rounded-md border-2 border-ink bg-cream px-2 py-0.5 font-mono text-[12px] shadow-pop-sm">{k}</kbd></Row>)}
          </div>
        )}
        <div className="px-6 pb-6 text-[11px] text-ink-soft"><Stamp size={18} /> SOVA Console</div>
      </div>
    </div>
  );
}

