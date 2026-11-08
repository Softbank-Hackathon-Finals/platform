const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sourceNames={aws_network:'AWS · VPC / EKS',source_k8s:'Source · EKS',platform_k8s:'Platform · RKE2',target_k8s:'Target · RKE2',tailscale_human:'기존 공유망',tailscale_workload:'Bravo Clusters',local_underlay:'Mac / VM 언더레이',platform_api:'Platform 운영자 API',target_host:'Target 호스트 NAT'};
const clusterFor={aws_network:'source_k8s',source_k8s:'source_k8s',platform_k8s:'platform_k8s',target_k8s:'target_k8s',platform_api:'platform_k8s',target_host:'target_k8s'};
const errorNames={TIMEOUT:'조회 시간 초과',AUTH_EXPIRED:'인증 만료',FORBIDDEN:'조회 권한 거부',CONNECTION_FAILED:'연결 실패',NOT_CONFIGURED:'조회 경로 미연결',PARTIAL_READ:'일부 조회 실패',PROVIDER_ERROR:'공급자 / 데이터 오류',IDENTITY_MISMATCH:'대상 신원 불일치'};
const categories={all:'확인 지점',paused:'수집 일시 중지',read:'조회 실패',stale:'오래된 관측',unknown:'경로 미확인',readiness:'준비 상태',history:'실패 종료 기록'};

function sourceState(source,now){
 if(['fresh','error','stale','unknown'].includes(source.freshness))return source.freshness;
 if(!source.checked_at||!Number.isFinite(Date.parse(source.checked_at)))return 'unknown';
 if(now-Date.parse(source.checked_at)>300000)return 'stale';
 return source.status==='error'?'error':'fresh';
}
function knownTransport(link){return ['DIRECT','DIRECT UDP','DERP','DERP RELAY','PEER RELAY','PEER_RELAY'].includes(String(link.state||'').trim().toUpperCase())}
function clusterFromEndpoint(value){const m=String(value||'').match(/^(Source|Platform|Target)\s*\//i);return m?m[1].toLowerCase()+'_k8s':null}

export function summarizeDiagnostics(snapshot,now=Date.now()){
 const sources=snapshot?.sources||[],issues=[],counts={paused:0,read:0,stale:0,unknown:0,readiness:0,history:0};
 const clusters=Object.fromEntries(['source_k8s','platform_k8s','target_k8s'].map(id=>[id,{paused:0,read:0,stale:0,unknown:0,readiness:0,history:0}]));
 let fresh=0,readyServices=0,totalServices=0,endpointServices=0,dnsAliasServices=0,unknownEndpointServices=0,readyPods=0,totalPods=0,direct=0,relay=0;
 function add(issue){issues.push(issue);counts[issue.category]++;if(clusters[issue.cluster])clusters[issue.cluster][issue.category]++}
 for(const source of sources){
  const freshness=sourceState(source,now),base={source:source.id,cluster:clusterFor[source.id]||null,checked_at:source.checked_at,last_success_at:source.last_success_at,source_name:sourceNames[source.id]||source.id};
  if(freshness==='fresh')fresh++;
  // Failure and age are independent facts: an old failed read remains a failure.
  if(source.status==='error')add({...base,id:'read:'+source.id,category:source.scope?.includes('SSM collection paused for shared host recovery')?'paused':'read',panel:'health',title:base.source_name,detail:source.scope?.includes('SSM collection paused for shared host recovery')?'복구 대기 · 수집 일시 중지':errorNames[source.error_code]||source.error_code||'조회 실패',note:source.scope?.includes('SSM collection paused for shared host recovery')?'공유 호스트 복구를 위해 수집을 일시 중지했습니다. 마지막 관측값을 보존합니다.':'수집 경로의 실패입니다. 서비스 중단을 의미하지 않습니다.'});
  if(freshness==='stale'||freshness==='unknown')add({...base,id:'age:'+source.id,category:'stale',panel:'health',title:base.source_name,detail:freshness==='unknown'?'성공 기록 없음':'5분을 넘긴 관측',note:'현재 상태를 판정할 수 없습니다. 마지막 성공값을 확인하세요.'});
  if(freshness!=='fresh'||source.retained)continue;
  for(const link of source.data?.links||[]){
   if(knownTransport(link)){if(/DIRECT/i.test(link.state||''))direct++;else relay++;continue}
   const key=[source.id,link.from,link.to,link.transport].join('|');
   add({...base,id:'link:'+key,category:'unknown',panel:'routes',cluster:clusterFromEndpoint(link.from)||base.cluster,linkKey:key,title:link.from+' → '+link.to,detail:link.state||'UNKNOWN',note:'탐침 결과가 충분하지 않습니다. Direct·DERP 또는 앱 장애로 추정하지 않습니다.',evidence:link.detail});
  }
  for(const resource of source.data?.resources||[]){
   const title=(resource.namespace?resource.namespace+'/':'')+resource.name,common={...base,id:['resource',source.id,resource.kind,resource.namespace,resource.name].join(':'),panel:'resources',title,resource:resource.name,kind:resource.kind,detail:resource.status,evidence:resource.detail};
   if(resource.kind==='Service'){
    totalServices++;
    if(resource.status==='DNS_ALIAS')dnsAliasServices++;
    else endpointServices++;
    if(resource.status==='UNKNOWN_ENDPOINTS')unknownEndpointServices++;
    if(resource.status==='READY_ENDPOINTS')readyServices++;
    if(resource.status==='NO_READY_ENDPOINTS')add({...common,category:'readiness',note:'Ready endpoint가 없습니다. 의도된 빈 Service인지, 워크로드 준비 실패인지 확인하세요.'});
   }
   if(resource.kind==='Pod'){
    if(resource.status==='Succeeded')continue;
    if(resource.status==='Failed'){add({...common,category:'history',note:'실패로 종료된 Pod 기록입니다. 현재 서비스 장애와는 별도로 확인합니다.'});continue}
    totalPods++;if(resource.status==='Ready')readyPods++;
    else add({...common,category:'readiness',note:'실행 중인 Pod의 준비 완료가 확인되지 않았습니다.',ready:resource.ready});
   }
   if(resource.kind==='Node'&&resource.status==='NotReady')add({...common,category:'readiness',note:'노드 Ready 조건이 충족되지 않았습니다. 클러스터 상세를 확인하세요.'});
  }
 }
 return {sources:sources.length,fresh,counts,issues,clusters,readyServices,totalServices,endpointServices,dnsAliasServices,unknownEndpointServices,readyPods,totalPods,direct,relay,received_at:snapshot?.received_at};
}

export function mountDiagnostics(){
 const tabs=document.querySelector('.live-workspace-tabs'),workbench=document.querySelector('.atlas-workbench');
 if(!tabs||!workbench||document.getElementById('atlas-diagnostics'))return;
 const strip=document.createElement('div');strip.id='atlas-diagnostics';strip.className='diagnostic-strip';strip.setAttribute('aria-label','관측 진단 요약');tabs.after(strip);
 const drawer=document.createElement('section');drawer.className='diagnostic-drawer';drawer.id='diagnostic-drawer';drawer.hidden=true;drawer.setAttribute('role','dialog');drawer.setAttribute('aria-modal','false');drawer.setAttribute('aria-labelledby','diagnostic-title');
 drawer.innerHTML='<header><div><span class="eyebrow">관측 근거</span><h2 id="diagnostic-title" tabindex="-1">확인 지점</h2></div><button class="diagnostic-close" aria-label="진단 닫기">×</button></header><div class="diagnostic-filter" role="group" aria-label="진단 분류"></div><p class="diagnostic-boundary">조회 실패, 준비 상태, 전송 미확인을 구분합니다.</p><div class="diagnostic-items"></div>';
 workbench.append(drawer);
 let summary=summarizeDiagnostics(null),filter='all',trigger=null,lastStrip='',lastList='';
 const formatTime=value=>value?new Intl.DateTimeFormat(document.documentElement.lang||'ko',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value))+' KST':'성공 기록 없음';
 function close(){drawer.hidden=true;trigger?.setAttribute('aria-expanded','false');trigger?.focus({preventScroll:true})}
 function show(category,button){filter=category;trigger=button;drawer.hidden=false;button?.setAttribute('aria-expanded','true');renderList();drawer.querySelector('h2').focus({preventScroll:true})}
 function renderList(){
  const rows=summary.issues.filter(issue=>filter==='all'||issue.category===filter);
  const list=rows.slice(0,80).map(issue=>`<button class="diagnostic-item ${issue.category}" data-diagnostic-id="${esc(issue.id)}"><span class="diagnostic-item-type">${categories[issue.category]} · ${esc(issue.kind||issue.source_name)}</span><strong>${esc(issue.title)}</strong><span class="diagnostic-item-status">${esc(issue.detail)}${issue.ready!==undefined?' · Ready '+esc(issue.ready):''}</span><span class="diagnostic-item-note">${issue.note}</span><small><span>마지막 시도</span> ${formatTime(issue.checked_at)}<br><span>마지막 성공</span> ${formatTime(issue.last_success_at)}</small><span class="diagnostic-open">상세 확인 →</span></button>`).join('')||'<div class="diagnostic-empty"><strong>해당 범위의 확인 지점이 없습니다.</strong><p>관측 범위 내 결과이며, 전체 서비스 정상 판정은 아닙니다.</p></div>';
  const html=list+(rows.length>80?'<p class="diagnostic-limit">80개까지 표시합니다. 해당 리소스 목록에서 전체를 확인하세요.</p>':'');
  if(html!==lastList){drawer.querySelector('.diagnostic-items').innerHTML=html;lastList=html}
  drawer.querySelector('.diagnostic-filter').innerHTML=Object.entries(categories).filter(([key])=>key==='all'||summary.counts[key]).map(([key,label])=>`<button data-diagnostic-filter="${key}" aria-pressed="${filter===key}">${label} <b>${key==='all'?summary.issues.length:summary.counts[key]}</b></button>`).join('');
 }
 function render(){
  const c=summary.counts,html=`<span class="diagnostic-coverage"><strong>${summary.fresh}/${summary.sources}</strong><span>최근 수집</span></span><span class="diagnostic-service" title="DNS 별칭은 Ready endpoint 집계에서 제외합니다."><strong>${summary.readyServices}/${summary.endpointServices}</strong><span>Ready Service</span></span><span class="diagnostic-alias"><strong>${summary.dnsAliasServices}</strong><span>DNS 별칭</span></span><span class="diagnostic-transport"><strong>${summary.direct} / ${summary.relay}</strong><span>Direct / Relay</span></span>${[...(c.paused?[['paused','수집 일시 중지']]:[]),['read','조회 실패'],['stale','오래됨'],['unknown','경로 미확인'],['readiness','준비 확인']].map(([key,label])=>`<button class="diagnostic-count ${key} ${c[key]?'has-items':''}" data-diagnostic-open="${key}" aria-controls="diagnostic-drawer" aria-expanded="false"><strong>${c[key]}</strong><span>${label}</span></button>`).join('')}<button class="diagnostic-all" data-diagnostic-open="all" aria-controls="diagnostic-drawer" aria-expanded="false">확인 지점 <b>${summary.issues.length}</b> →</button>`;
  if(html!==lastStrip){strip.innerHTML=html;lastStrip=html;if(!drawer.hidden){trigger=strip.querySelector('[data-diagnostic-open="'+filter+'"]')||strip.querySelector('[data-diagnostic-open="all"]');trigger?.setAttribute('aria-expanded','true')}}
  if(!drawer.hidden)renderList();
 }
 strip.addEventListener('click',event=>{const button=event.target.closest('[data-diagnostic-open]');if(button)show(button.dataset.diagnosticOpen,button)});
 drawer.querySelector('.diagnostic-close').addEventListener('click',close);
 drawer.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();close()}});
 drawer.addEventListener('click',event=>{
  const tab=event.target.closest('[data-diagnostic-filter]');if(tab){filter=tab.dataset.diagnosticFilter;renderList();return}
  const row=event.target.closest('[data-diagnostic-id]');if(!row)return;const issue=summary.issues.find(x=>x.id===row.dataset.diagnosticId);if(!issue)return;
  close();window.dispatchEvent(new CustomEvent('atlas-inspect',{detail:{panel:issue.panel,source:issue.source,cluster:issue.cluster,linkKey:issue.linkKey,resource:issue.resource,kind:issue.kind}}));
 });
 document.addEventListener('pointerdown',event=>{if(!drawer.hidden&&!drawer.contains(event.target)&&!strip.contains(event.target)){drawer.hidden=true;trigger?.setAttribute('aria-expanded','false')}});
 window.addEventListener('atlas-status',event=>{const snapshot=event.detail;summary=summarizeDiagnostics(snapshot,Date.parse(snapshot.server_now)||Date.now());render()});
 window.addEventListener('atlas-locale',()=>{lastStrip='';lastList='';render()});render();
}
