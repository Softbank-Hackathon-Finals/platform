import {networkURL, clearNetworkScope, showNetworkScope} from './bridge.js';
import {mountMapControls,mountLiveWorkspace} from './map-controls.js';
import {mountDiagnostics,summarizeDiagnostics} from './diagnostics.js';
const $=s=>document.querySelector(s);
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={aws_network:'AWS · VPC / EKS',platform_k8s:'Platform · RKE2 전체',target_k8s:'Target · RKE2 전체',source_k8s:'Source · EKS 전체',tailscale_human:'Tailscale · 기존 공유망',tailscale_workload:'Tailscale · 워크로드',local_underlay:'Mac / VM · Underlay',platform_api:'Platform · 운영자 API',target_host:'Target · 호스트 NAT'};
const errors={TIMEOUT:'조회 시간 초과',AUTH_EXPIRED:'인증 만료',FORBIDDEN:'조회 권한 거부',CONNECTION_FAILED:'연결 실패',NOT_CONFIGURED:'조회 경로 미연결',PARTIAL_READ:'일부 조회 실패',PROVIDER_ERROR:'공급자 / 데이터 오류',IDENTITY_MISMATCH:'대상 신원 불일치'};
const isPaused=s=>s?.scope?.includes('SSM collection paused for shared host recovery');
const sourceError=s=>isPaused(s)?'복구 대기 · 수집 일시 중지':errors[s.error_code]||s.error_code;
const names={fresh:'최근 조회',error:'조회 실패',stale:'오래된 결과',unknown:'조회 없음'};
const time=s=>s?new Date(s).toLocaleString(document.documentElement.lang||'ko',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' KST':'성공 기록 없음';
export function freshness(s,now){return !s?.checked_at?'unknown':now-Date.parse(s.checked_at)>300000?'stale':s.status==='error'?'error':'fresh'}
let data=null,offset=0,browserError=false,busy=false,lastFetch=null;
let socket=null,socketState='connecting',lastSocket=null,socketOpened=null,reconnect=0,retryTimer=null;
const transportLabel=typeof location!=='undefined'&&location.protocol==='https:'?'WSS':'WS';
const socketLabels={connecting:`${transportLabel} 연결 중`,open:`${transportLabel} 연결됨`,reconnecting:`${transportLabel} 재연결 중`,offline:'오프라인',error:`${transportLabel} 수신 오류`};
function sourceClass(s){return freshness(s,Date.now()+offset)}
function chip(state,label){return `<span class="health-chip ${state}">${escape(label||names[state]||state)}</span>`}
function rows(kind){return (data?.sources||[]).flatMap(s=>(s.data?.[kind]||[]).map(row=>({...row,source:s.id,fresh:sourceClass(s),checked_at:s.retained?s.last_success_at:s.checked_at,attempt_at:s.checked_at,retained:s.retained})));}
function renderResources(){
 let list=rows('resources'),source=$('#resource-source').value,kind=$('#resource-kind').value,q=$('#resource-search').value.toLowerCase();
 list=list.filter(x=>(source==='all'||x.source===source)&&(kind==='all'||x.kind===kind)&&JSON.stringify(x).toLowerCase().includes(q));
 $('#resource-count').textContent=`${list.length}개 / ${rows('resources').length}개`;
 $('#live-resources').innerHTML=list.map(x=>`<tr class="row-${x.fresh}"><td>${escape(labels[x.source])}<small>${escape(x.namespace||'—')}</small>${chip(x.fresh)}</td><td><strong>${escape(x.name)}</strong><small>${escape(x.kind)}</small></td><td>${escape(x.status)}${x.ready!==undefined?`<small>Ready ${escape(x.ready)}</small>`:''}${x.retained?'<small>마지막 성공값 보존</small>':''}</td><td><code>${escape(x.ip||'—')}</code><small>${escape(x.ports||'')}</small></td><td>${escape(x.detail)}<small>${time(x.checked_at)}</small></td></tr>`).join('')||'<tr><td colspan="5">이 범위에서 표시할 조회 결과가 없습니다.</td></tr>';
}
function renderL3(){
 const all=rows('networks'),layer=$('#l3-layer').value,source=$('#l3-source').value,q=$('#l3-search').value.toLowerCase();
 const groups=[['underlay','01 / UNDERLAY','VPC · LAN · VM','실제 인터페이스와 다음 홉'],['overlay','02 / OVERLAY','Tailscale · Pod · Service','언더레이 위에서 사용하는 논리 주소'],['translation','03 / TRANSLATION','NAT · SNAT · DNAT','주소 변환의 설정과 관측 범위']];
 $('#l3-stack').innerHTML=groups.map(([id,k,title,desc])=>`<article class="l3-card ${id}"><span class="eyebrow">${k}</span><h2>${title}</h2><p>${desc}</p>${all.filter(x=>x.layer===id).slice(0,4).map(x=>`<div><strong>${escape(x.name)}</strong><code>${escape(id==='translation'?x.nat:x.cidr)}</code>${chip(x.fresh)}</div>`).join('')||'<p>아직 조회된 값이 없습니다.</p>'}<small>${all.filter(x=>x.layer===id).length}개 항목 · 전체 상세는 아래 표</small></article>`).join('');
 $('#l3-rows').innerHTML=all.filter(x=>(layer==='all'||x.layer===layer)&&(source==='all'||x.source===source)&&JSON.stringify(x).toLowerCase().includes(q)).map(x=>`<tr class="row-${x.fresh}"><td><b>${escape(x.layer.toUpperCase())}</b><small>${escape(labels[x.source])}</small>${chip(x.fresh)}</td><td>${escape(x.name)}</td><td><code>${escape(x.cidr||'UNKNOWN')}</code></td><td><code>${escape(x.next_hop)}</code></td><td>${escape(x.nat)}</td><td>${escape(x.detail)}<small>확인 ${time(x.checked_at)}</small>${x.retained?'<small>이전 성공값 · 현재 정상 보장 안 됨</small>':''}</td></tr>`).join('')||'<tr><td colspan="6">해당 계층의 조회값이 없습니다.</td></tr>';
}
function render(){
 const now=Date.now()+offset,ss=data?.sources||[],stale=!data?.received_at||now-Date.parse(data.received_at)>300000,failed=ss.filter(s=>s.status==='error'&&!isPaused(s)),paused=ss.filter(isPaused),fresh=ss.filter(s=>sourceClass(s)==='fresh'),aged=ss.filter(s=>sourceClass(s)==='stale');
 $('#live-banner').className='live-banner '+(browserError||stale?'stale':failed.length||paused.length||aged.length?'warning':'fresh');
 $('#live-banner').innerHTML=`<span class="status-dot"></span><div><strong>${browserError?'상태 서버 조회 실패':stale?'수집 결과가 오래됐거나 아직 없습니다':paused.length?`${paused.length}개 수집 경로 복구 대기`:failed.length?`${failed.length}개 경로 조회 실패 / 미연결`:aged.length?`${aged.length}개 수집 결과가 오래됐습니다`:'자동 수집 중'}</strong><span>서버 수신 ${time(data?.received_at)} · 화면 확인 ${lastFetch?time(lastFetch):'대기'}${browserError?' · 마지막 결과를 보존합니다.':''}</span></div>`;
 $('#live-banner').innerHTML+=`<span class="socket-status ${socketState==='open'?'fresh':'stale'}">${socketLabels[socketState]}<small>소켓 수신 ${lastSocket?time(lastSocket):'대기'} · 화면 상태는 수집 시각 기준</small></span>`;
 $('#snapshot-date').textContent=data?.received_at?time(data.received_at):'조회 대기';
 const res=rows('resources');const active=res.filter(r=>r.kind==='Service'&&r.status==='READY_ENDPOINTS'&&r.fresh==='fresh').length;
 $('#live-summary').innerHTML=[[String(fresh.length).padStart(2,'0')+'/'+ss.length,'최근 성공한 수집 경로'],[fresh.some(s=>s.id.endsWith('_k8s'))?String(active).padStart(2,'0'):'—','최근 관측 Ready Service'],[String(res.filter(r=>r.kind==='Tailscale device'||r.kind==='Tailscale peer').length).padStart(2,'0'),'발견된 Tailscale 기기'],[String(rows('links').filter(l=>l.transport?.includes('workload')).length).padStart(2,'0'),'관측된 워크로드 경로']].map(([n,t])=>`<div><strong>${n}</strong><span>${t}</span></div>`).join('');
 $('#collection-overview').textContent=`${failed.length?failed.length+'개 조회 실패':'수집 경로 '+ss.length+'개'} · 마지막 확인 ${time(data?.received_at)}`;
 $('#source-health').innerHTML=ss.map(s=>`<article class="source-card ${sourceClass(s)}"><div><h3>${escape(labels[s.id])}</h3>${chip(sourceClass(s),s.status==='error'?sourceError(s):undefined)}</div><p>${escape(s.scope)}</p><dl><dt>마지막 시도</dt><dd>${time(s.checked_at)}</dd><dt>마지막 성공</dt><dd>${time(s.last_success_at)}</dd><dt>조회 소요</dt><dd>${(s.duration_ms/1000).toFixed(1)}s${s.retained?' · 이전 성공값 보존':''}</dd></dl></article>`).join('')||'<p>첫 수집 결과를 기다립니다.</p>';
 renderTransport();renderTopology();
 renderActivity(now);
 $('#live-gaps').innerHTML='<h3>실측의 범위</h3><p>인프라는 120초마다 읽고 5분이 지나면 오래된 결과로 표시합니다. Direct·DERP는 표시된 프록시의 탐침 결과입니다. NAT 규칙·카운터, 서비스 Ready, 데이터 트랜잭션 성공은 각각 다른 증거입니다. Mac이 꺼지거나 조회에 실패하면 마지막 성공값과 실패 시각을 함께 남깁니다.</p>';
 renderResources();renderL3();
 window.dispatchEvent(new CustomEvent('atlas-status',{detail:{...data,sources:ss.map(s=>({...s,freshness:sourceClass(s)}))}}));
}
function renderActivity(now){
 const target=$('#integration-events');if(!target)return;
 const stages={preflight:'실행 준비',input:'입력 연결',proposal:'제안 생성',rehearsal:'리허설',approval:'승인',deploy:'배포',verify:'검증',cutover:'컷오버',cleanup:'정리'};
 const statuses={waiting:'대기',running:'진행',blocked:'차단',pass:'통과',fail:'실패',done:'완료',unknown:'미확인'};
 const reasons={NONE:'',WAITING_INPUT:'입력 대기',WAITING_REVIEW:'검토 대기',WAITING_CI:'CI 대기',WAITING_WINDOW:'실행 창 대기',AUTH_DENIED:'권한 거부',CONNECTION_FAILED:'연결 실패',VALIDATION_FAILED:'검증 실패',DEPENDENCY_BLOCKED:'선행 작업 차단',EXECUTION_FAILED:'실행 실패',COMPLETE:'완료',UNKNOWN:'원인 미확인'};
 const events=data?.activity||[];
 target.innerHTML=events.length?events.map(e=>`<article class="source-card ${['blocked','fail'].includes(e.status)?'error':now-Date.parse(e.observed_at)>300000?'stale':'fresh'}"><div><h3>${escape(stages[e.stage])}</h3><strong>${escape(statuses[e.status])}</strong></div><code>${escape(e.run_id)}</code><p>${escape(reasons[e.reason_code])}</p><small>관측 ${time(e.observed_at)}${now-Date.parse(e.observed_at)>300000?' · 이전 기록':''}<br>근거 ${escape(e.receipt_id)}</small></article>`).join(''):`<div class="scope-note compact">${data?.activity_status==='invalid'?'실행 담당의 상태 파일 형식이 맞지 않습니다. 마지막 실행 결과를 추정하지 않습니다.':'실행 담당의 상태 영수증이 아직 연결되지 않았습니다. 클러스터 Ready를 E2E 실행 완료로 표시하지 않습니다.'}</div>`;
}
function accept(d){showNetworkScope(d);if(!Array.isArray(d.sources))throw Error('SCHEMA');data=d;offset=Date.parse(d.server_now)-Date.now();lastFetch=new Date().toISOString();browserError=false;render()}
function connect(){
 clearTimeout(retryTimer);
 if(socket&&[WebSocket.OPEN,WebSocket.CONNECTING].includes(socket.readyState))return;
 if(!navigator.onLine){socketState='offline';render();return}
 socketState=reconnect?'reconnecting':'connecting';render();
 const ws=new WebSocket(networkURL('stream',true));socket=ws;
 ws.onopen=()=>{socketState='open';socketOpened=Date.now();reconnect=0;render()};
 ws.onmessage=event=>{try{const m=JSON.parse(event.data);lastSocket=new Date().toISOString();socketState='open';if(m.type==='snapshot')accept(m.data);else if(m.type==='error'){data=null;clearNetworkScope('관측 권한·등록·수집 상태를 확인할 수 없어 자료를 지웠습니다.');browserError=true;socketState='error'}render()}catch{socketState='error';render()}};
 ws.onerror=()=>{socketState='error';render()};
 ws.onclose=event=>{if([4401,4403].includes(event.code)){data=null;browserError=true;socketState='error';clearNetworkScope();render();return}if(socket!==ws)return;socket=null;socketState=navigator.onLine?'reconnecting':'offline';render();const delay=Math.min(30000,1000*2**Math.min(reconnect++,5));retryTimer=setTimeout(connect,delay)};
}
async function refresh(){
 if(busy)return;busy=true;$('#refresh-live').disabled=true;
 try{const r=await fetch(networkURL('status'),{cache:'no-store',signal:AbortSignal.timeout(10000)});if(!r.ok){data=null;clearNetworkScope([401,403].includes(r.status)?undefined:'관측 권한·등록·수집 상태를 확인할 수 없어 자료를 지웠습니다.');render();throw Error('HTTP')}accept(await r.json());}
 catch{browserError=true}finally{busy=false;$('#refresh-live').disabled=false;render()}
}
let atlasCluster=null,atlasLink=null,atlasRoute=null,mapLayer='all',walkStep=-1,walkTimer=null;
const clusterInfo=[{id:'source_k8s',name:'Source',subtitle:'AWS · EKS',x:38,y:52},{id:'platform_k8s',name:'Platform',subtitle:'AWS · RKE2',x:38,y:310},{id:'target_k8s',name:'Target',subtitle:'Mac VM · RKE2',x:726,y:182}];
const pathKey=l=>[l.source,l.from,l.to,l.transport].join('|');
function pathState(l){const s=(l?.state||'').toUpperCase();return s.includes('DERP')?'DERP':s.includes('PEER')&&s.includes('RELAY')?'PEER_RELAY':s.includes('DIRECT')?'DIRECT':'UNKNOWN'}
function pathStateLabel(l){return ({DIRECT:'Direct · UDP',DERP:'DERP · 릴레이',PEER_RELAY:'Peer relay',UNKNOWN:'미확인'})[pathState(l)]}
function stateChip(l){const s=pathState(l);return `<span class="transport-state ${s.toLowerCase()}"><i></i>${pathStateLabel(l)}</span>`}
function currentLink(){return rows('links').find(l=>pathKey(l)===atlasLink)}
function selectLink(key){atlasLink=key;atlasCluster=null;atlasRoute=null;walkStep=-1;clearInterval(walkTimer);walkTimer=null;renderTransport();renderTopology()}
function renderTransport(){
 let list=rows('links');const source=$('#transport-source').value,kind=$('#transport-state').value,q=$('#transport-search').value.toLowerCase();
 list=list.filter(l=>(source==='all'||(source==='workload')===!!l.transport?.toLowerCase().includes('workload'))&&(kind==='all'||pathState(l)===kind)&&JSON.stringify(l).toLowerCase().includes(q));
 $('#transport-count').textContent=`${list.length}개`;
 const tableHTML=list.length?list.map(l=>`<tr class="${pathKey(l)===atlasLink?'selected ':''}row-${l.fresh}" tabindex="0" data-link-key="${escape(pathKey(l))}" aria-label="${escape(l.from+'에서 '+l.to+' 경로 상세')}"><td><strong>${escape(l.from)}</strong><small>${escape(labels[l.source]||l.source)}</small></td><td><strong>${escape(l.to)}</strong><small>${escape(l.transport)}</small></td><td>${stateChip(l)}${l.fresh!=='fresh'?chip(l.fresh):''}</td><td>${time(l.checked_at)}${l.retained?'<small>이전 성공값</small>':''}</td><td><span class="row-detail">${escape(l.detail)}</span><span class="row-open">상세 →</span></td></tr>`).join(''):'<tr><td colspan="5">이 조건에서 관측된 경로가 없습니다.</td></tr>';
 if(renderTransport.lastHTML===tableHTML)return;renderTransport.lastHTML=tableHTML;$('#live-links').innerHTML=tableHTML;
 $('#live-links').querySelectorAll('[data-link-key]').forEach(row=>{const choose=()=>selectLink(row.dataset.linkKey);row.addEventListener('click',choose);row.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();choose()}})});
}
function clusterData(id){return data?.sources?.find(s=>s.id===id)}
function clusterStats(id){const s=clusterData(id),r=s?.data?.resources||[];return {source:s,pods:s?.data?r.filter(r=>r.kind==='Pod').length:'—',services:s?.data?r.filter(r=>r.kind==='Service').length:'—',ready:s?.data?r.filter(r=>r.kind==='Service'&&r.status==='READY_ENDPOINTS').length:'—',node:r.find(r=>r.kind==='Node'),fresh:s?sourceClass(s):'unknown'}}
function mapState(which){const current=currentLink();if(current&&((which==='workload')===current.detail?.includes('tail63abf4')))return pathState(current);return 'UNKNOWN'}
export function endpointCluster(value){const v=(value||'').toLowerCase(),prefix=v.match(/^(source|platform|target)\s*\//);return prefix?prefix[1]:v.includes('target')?'target':v.includes('source')?'source':v.includes('platform')||v.includes('metrics')||v.includes('prometheus')||v.includes('intake')?'platform':v.includes('mac')?'mac':null}
export function measuredPath(l){
 if(!l)return '';const a=endpointCluster(l.from),b=endpointCluster(l.to),cy=l.detail?.includes('tail63abf4')?381:123;
 // Route around the other tailnet box: the two identity boundaries are not transit gateways.
 const leg=(name,offset)=>{const y=cy+offset;return ({source:[[262,123],[310,123],[310,y],[388,y],[500,cy]],platform:[[262,381],[340,381],[340,y],[388,y],[500,cy]],target:[[726,254],[678,254],[678,y],[608,y],[500,cy]],mac:[[510,474],[650,474],[650,y],[608,y],[500,cy]]})[name]};
 const start=leg(a,-12),end=leg(b,12);if(!start||!end)return '';
 return [...start,...end.slice(0,-1).reverse()].map(([x,y],i)=>`${i?'L':'M'}${x} ${y}`).join('');
}
function probeFields(l){return Object.fromEntries((l?.detail||'').split(';').map(s=>s.trim().split(/=(.*)/s).slice(0,2)).filter(v=>v.length===2))}
function renderTopology(){
 if($('#atlas-viewport')?.dataset.gesture==='active')return;
 const diagnostics=summarizeDiagnostics(data,Date.now()+offset);
 const l=currentLink(),focused=!!(atlasCluster||l),isWork=l?.detail?.includes('tail63abf4'),activePath=measuredPath(l);
 const wire=(id,d,kind)=>`<g class="map-route" data-map-route="${kind}" role="button" tabindex="0" aria-label="${kind==='human'?'기존 공유망 경로 상세':'워크로드 API 경로 상세'}"><path class="atlas-wire ${kind} unknown ${focused?'faded':''}" id="atlas-${id}" d="${d}"/><path class="route-hit" d="${d}"/></g>`;
 const nets=rows('networks'),physical=nets.find(n=>n.name==='Collector physical network'),guest=nets.find(n=>n.source==='target_host'&&n.layer==='underlay'&&n.cidr?.includes('10.0.2'));
 const topologyHTML=`<defs><pattern id="atlas-grid" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".65" class="grid-dot"/></pattern></defs><rect width="1000" height="540" fill="url(#atlas-grid)"/>
 <g class="map-underlay ${mapLayer==='overlay'?'layer-muted':''}"><path d="M150 197V310" class="physical-wire"/><path d="M150 455V490H860V325" class="physical-wire"/><text x="42" y="246" class="physical-label">Source ↔ Platform</text><text x="42" y="264" class="physical-label">VPC 피어링 · 사설 라우팅</text><text x="305" y="465" class="physical-label">NAT Gateway / 인터넷 / Mac / VM</text><text x="38" y="528" class="map-caption">VM 언더레이 ${escape(guest?.cidr||'호스트 상세에서 확인')} · 선은 접속 구성, 전송 판정은 경로별 실측</text><g class="mac-node"><rect x="400" y="474" width="220" height="34" rx="7"/><text x="416" y="495">Collector Mac · ${escape(physical?.cidr||'조회 대기')}</text></g></g>
 <g class="map-overlay ${mapLayer==='underlay'?'layer-muted':''}">${wire('human-left','M262 123H388','human')}${wire('human-right','M608 123H678V220H726','human')}${wire('work-left','M262 381H388','workload')}${wire('work-right','M608 381H678V289H726','workload')}
 ${l&&activePath?`<g class="map-route" data-map-link="${escape(pathKey(l))}" role="button" tabindex="0" aria-label="선택한 전송 경로 상세"><path class="atlas-wire selected-wire ${pathState(l).toLowerCase()} ${mapLayer==='underlay'?'layer-muted':''}" d="${activePath}"/><path class="route-hit" d="${activePath}"/></g>`:''}${l&&activePath&&mapLayer!=='underlay'&&walkStep===2&&pathState(l)!=='UNKNOWN'?`<circle r="5" class="atlas-packet"><animateMotion dur="2.4s" repeatCount="indefinite" path="${activePath}"/></circle>`:''}
 <g class="tailnet-node" data-map-route="human" role="button" tabindex="0" aria-label="기존 공유망 경로 상세"><rect x="388" y="84" width="220" height="78" rx="12"/><text x="410" y="109" class="tailnet-label">기존 공유망</text><text x="410" y="134" class="map-mono">운영자 · DB · 메트릭</text><text x="410" y="151" class="tailnet-sub">${escape(mapState('human')==='UNKNOWN'?'관측 경로별 상태 확인':mapState('human'))}</text></g>
 <g class="tailnet-node workload" data-map-route="workload" role="button" tabindex="0" aria-label="워크로드 API 경로 상세"><rect x="388" y="342" width="220" height="78" rx="12"/><text x="410" y="367" class="tailnet-label">Bravo Clusters</text><text x="410" y="392" class="map-mono">Rehearsal / Apply → API</text><text x="410" y="409" class="tailnet-sub">${escape(mapState('workload')==='UNKNOWN'?'관측 경로별 상태 확인':mapState('workload'))}</text></g>
 <g class="map-explanation ${l?'is-hidden':''}"><text x="400" y="215" class="map-center-caption">독립된 두 접근 경계</text><text x="400" y="238" class="map-center-note">Tailscale 신원 ≠ Kubernetes 권한</text><text x="400" y="261" class="map-center-note">Direct / DERP는 개별 경로에서 측정</text></g></g>
 ${clusterInfo.map(c=>{const st=clusterStats(c.id),issues=diagnostics.clusters[c.id],attention=issues.read+(issues.paused||0)+issues.stale+issues.unknown+issues.readiness;return `<g class="cluster-node ${atlasCluster===c.id?'selected':''} ${focused&&atlasCluster&&atlasCluster!==c.id?'faded':''}" transform="translate(${c.x} ${c.y})" role="button" tabindex="0" data-cluster="${c.id}" aria-label="${c.name} 클러스터 상세"><rect width="224" height="145" rx="12"/><rect class="cluster-icon-bg" x="16" y="17" width="34" height="34" rx="9"/><path class="cluster-glyph" d="M26 27h14v6H26zM26 36h14v6H26z"/><circle class="cluster-status ${st.fresh}" cx="198" cy="27" r="4"/><text x="62" y="35" class="cluster-title">${c.name}</text><text x="62" y="53" class="cluster-sub">${c.subtitle}</text>${attention?`<g class="cluster-attention"><title>조회·준비·전송 상태 확인 지점</title><rect x="154" y="40" width="57" height="20" rx="5"/><text x="182" y="54">확인 ${attention}</text></g>`:''}<text x="17" y="85" class="map-mono">${escape(st.node?.ip?.split(',')[0]||'노드 조회 대기')}</text><line x1="17" y1="101" x2="207" y2="101" class="cluster-divider"/><text x="17" y="124" class="cluster-metric">Pod <tspan>${st.pods}</tspan></text><text x="101" y="124" class="cluster-metric">Service <tspan>${st.services}</tspan></text><text x="17" y="160" class="cluster-time">${st.source?time(st.source.checked_at):'첫 수집 대기'}</text></g>`}).join('')}
`;
 if(renderTopology.lastHTML===topologyHTML){renderInspector();return}renderTopology.lastHTML=topologyHTML;$('#live-topology').innerHTML=topologyHTML;
 $('#live-topology').querySelectorAll('[data-cluster]').forEach(el=>{const choose=()=>{atlasCluster=el.dataset.cluster;atlasLink=null;atlasRoute=null;walkStep=-1;clearInterval(walkTimer);walkTimer=null;$('#resource-source').value=atlasCluster;renderResources();renderTopology();renderTransport()};el.addEventListener('click',choose);el.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();choose()}})});
 $('#live-topology').querySelectorAll('[data-map-route],[data-map-link]').forEach(el=>{const choose=()=>{if(el.dataset.mapLink){selectLink(el.dataset.mapLink);return}atlasRoute=el.dataset.mapRoute;atlasCluster=null;atlasLink=null;walkStep=-1;clearInterval(walkTimer);walkTimer=null;renderTopology();renderTransport()};el.addEventListener('click',choose);el.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();choose()}})});
 renderInspector();
}
function renderInspector(){
 const l=currentLink();let html='';
 if(atlasRoute){
  const workload=atlasRoute==='workload',list=rows('links').filter(l=>!!l.detail?.includes('tail63abf4')===workload);
  html=`<div class="inspector-heading"><span class="eyebrow">선택한 네트워크</span><h2>${workload?'Bravo Clusters':'기존 공유망'}</h2><p>${workload?'워크로드 API':'운영자 · DB · 메트릭'} · ${list.length}개 관측 경로</p></div><div class="route-list">${list.map(l=>`<button data-inspect-link="${escape(pathKey(l))}"><strong>${escape(l.from)}</strong><small>→ ${escape(l.to)}</small>${stateChip(l)}</button>`).join('')||'<p>관측된 경로가 없습니다.</p>'}</div>`;
 }else if(l){
  const fields=probeFields(l),steps=['워크로드 / 기기','서비스와 프록시','암호화된 전송',pathState(l)==='UNKNOWN'?'도달 여부 미확인':'목적지 피어 응답'],stepDetails=[l.from,fields.Service||'운영자 기기 · 기존 공유망',`${pathStateLabel(l)}${fields.RTT?' · '+fields.RTT:''}`,fields.peer||l.to];
  html=`<div class="inspector-heading"><span class="eyebrow">선택한 전송 경로</span><h2>${escape(l.to)}</h2>${stateChip(l)}</div><dl class="inspector-facts"><dt>출발</dt><dd>${escape(l.from)}</dd><dt>전송</dt><dd>${escape(l.transport)}</dd><dt>확인 시각</dt><dd>${time(l.checked_at)}</dd><dt>데이터 상태</dt><dd>${names[l.fresh]||l.fresh}${l.retained?' · 마지막 성공값':''}</dd>${[['서비스',fields.Service],['목적 주소',fields.FQDN],['피어',fields.peer],['Tailnet',fields.tailnet],['왕복 지연',fields.RTT],['응답 수',fields['응답']]].filter(x=>x[1]).map(([k,v])=>`<dt>${k}</dt><dd><code>${escape(v)}</code></dd>`).join('')}</dl><div class="inspector-section"><h3>측정 근거</h3><details class="probe-receipt"><summary>탐침 원문 확인</summary><p class="probe-evidence">${escape(l.detail)}</p></details></div><div class="inspector-section"><div class="inspector-playhead"><h3>경로 따라가기</h3><button id="trace-play" class="small-button">${walkTimer?'일시정지':'재생'}</button><button id="trace-step" class="text-button">다음</button></div><ol class="trace-steps">${steps.map((s,i)=>`<li class="${i===walkStep?'current':i<walkStep?'done':''}"><span>${String(i+1).padStart(2,'0')}</span><div>${s}${i===walkStep?`<small>${escape(stepDetails[i])}</small>`:''}</div></li>`).join('')}</ol><p class="inspector-note">구성 설명용 재생입니다. 실제 패킷 캡처가 아닙니다. API 권한과 앱 응답은 별도로 확인합니다.</p></div>`;
 }else if(atlasCluster){
  const c=clusterInfo.find(c=>c.id===atlasCluster),st=clusterStats(atlasCluster),s=st.source,nets=rows('networks').filter(n=>n.source===atlasCluster||(atlasCluster==='target_k8s'&&n.source==='target_host'));
  html=`<div class="inspector-heading"><span class="eyebrow">클러스터 상세</span><h2>${c.name}</h2><p>${c.subtitle} ${chip(st.fresh)}</p></div><dl class="inspector-facts"><dt>노드</dt><dd>${escape(st.node?.name||'조회 대기')}</dd><dt>노드 주소</dt><dd><code>${escape(st.node?.ip||'UNKNOWN')}</code></dd><dt>서비스</dt><dd>${st.services}개 · Ready endpoint ${st.ready}개</dd><dt>마지막 시도</dt><dd>${time(s?.checked_at)}</dd><dt>마지막 성공</dt><dd>${time(s?.last_success_at)}</dd>${s?.status==='error'?`<dt>조회 실패</dt><dd>${escape(sourceError(s))}</dd><dt>조회 범위</dt><dd>${escape(s.scope)}</dd>`:''}</dl><div class="inspector-section"><h3>네트워크 관측</h3>${nets.slice(0,4).map(n=>`<div class="inspect-network"><span>${escape(n.name)}</span><code>${escape(n.cidr||n.nat)}</code><small>${escape(n.next_hop)}</small></div>`).join('')||'<p>해당 클러스터의 주소망 조회를 기다립니다.</p>'}<button id="inspect-l3" class="inline-link">라우팅 · NAT 전체 상세 →</button></div><div class="inspector-section"><h3>조회 범위</h3><p>${escape(s?.scope||'수집 대기')}</p><p class="inspector-note">아래 리소스 목록도 이 클러스터로 필터링했습니다.</p></div>`;
 }else{
  const ss=data?.sources||[],fail=ss.filter(s=>s.status==='error');html=`<div class="inspector-heading"><span class="eyebrow">현재 환경</span><h2>3개 클러스터의 연결</h2><p>구성요소를 눌러 주소, 서비스와 관측 근거를 살펴보세요.</p></div><div class="inspector-section cluster-summary">${clusterInfo.map(c=>{const st=clusterStats(c.id);return `<button data-inspect-cluster="${c.id}"><span><strong>${c.name}</strong><small>${st.services} Services · ${st.pods} Pods</small></span>${chip(st.fresh)}<span>›</span></button>`}).join('')}</div><div class="inspector-section"><h3>${fail.length?'확인이 필요한 조회':'갱신 상태'}</h3>${fail.length?fail.map(s=>`<p class="inspector-error">${escape(labels[s.id]||s.id)} · ${escape(sourceError(s))}</p>`).join(''):'<p>수집이 끝나면 열린 지도와 경로가 자동으로 바뀝니다.</p>'}<dl class="inspector-facts"><dt>서버 수신</dt><dd>${time(data?.received_at)}</dd><dt>수집 간격</dt><dd>120초 · 5분 초과 시 오래됨</dd></dl></div><div class="inspector-section"><h3>전송 경로</h3><p>지도 연결선 또는 전송 경로에서 워크로드를 선택하세요.</p><button class="inline-link" data-open-live-panel="routes">전송 경로 보기 →</button></div>`;
 }
 if(renderInspector.lastHTML===html)return;renderInspector.lastHTML=html;$('#live-inspector').innerHTML=html;
 $('#live-inspector').querySelectorAll('[data-inspect-cluster]').forEach(b=>b.addEventListener('click',()=>{atlasCluster=b.dataset.inspectCluster;atlasLink=null;atlasRoute=null;$('#resource-source').value=atlasCluster;renderResources();renderTopology()}));
 $('#live-inspector').querySelectorAll('[data-inspect-link]').forEach(b=>b.addEventListener('click',()=>selectLink(b.dataset.inspectLink)));
 $('#inspect-l3')?.addEventListener('click',()=>{document.querySelector('[data-view="l3"]').click();$('#l3-source').value=atlasCluster==='target_k8s'?'target_host':atlasCluster;renderL3()});
 $('#trace-play')?.addEventListener('click',()=>{if(walkTimer){clearInterval(walkTimer);walkTimer=null;renderInspector();return}walkStep=0;walkTimer=setInterval(()=>{walkStep++;if(walkStep>3){clearInterval(walkTimer);walkTimer=null;walkStep=3}renderTopology()},1800);renderTopology()});
 $('#trace-step')?.addEventListener('click',()=>{clearInterval(walkTimer);walkTimer=null;walkStep=(walkStep+1)%4;renderTopology()});
}

if(typeof document!=='undefined'){
 mountLiveWorkspace();mountMapControls();mountDiagnostics();
 window.addEventListener('atlas-locale',()=>{if(data)render()});
 window.addEventListener('atlas-inspect',event=>{const d=event.detail;if(d.linkKey){$('#transport-source').value='all';$('#transport-state').value='all';$('#transport-search').value='';selectLink(d.linkKey)}else{atlasCluster=d.cluster;atlasLink=null;atlasRoute=null;if(d.panel==='resources'){$('#resource-source').value=d.source;$('#resource-kind').value=d.kind||'all';$('#resource-search').value=d.resource||'';renderResources()}renderTopology()}});
 for(const id of ['transport-source','transport-state'])$('#'+id).addEventListener('change',renderTransport);$('#transport-search').addEventListener('input',renderTransport);$('#atlas-reset').addEventListener('click',()=>{atlasCluster=null;atlasLink=null;atlasRoute=null;walkStep=-1;clearInterval(walkTimer);walkTimer=null;$('#resource-source').value='all';renderResources();renderTopology();renderTransport()});document.querySelectorAll('[data-map-layer]').forEach(b=>b.addEventListener('click',()=>{mapLayer=b.dataset.mapLayer;document.querySelectorAll('[data-map-layer]').forEach(x=>x.classList.toggle('selected',x===b));renderTopology()}));
 for(const [id,label] of Object.entries(labels)){for(const selector of ['#resource-source','#l3-source']){const o=document.createElement('option');o.value=id;o.textContent=label;$(selector).append(o)}}
 $('#resource-source').addEventListener('change',renderResources);$('#resource-kind').addEventListener('change',renderResources);$('#resource-search').addEventListener('input',renderResources);$('#l3-layer').addEventListener('change',renderL3);$('#l3-source').addEventListener('change',renderL3);$('#l3-search').addEventListener('input',renderL3);$('#refresh-live').addEventListener('click',refresh);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)connect()});
 window.addEventListener('online',connect);window.addEventListener('offline',()=>{socketState='offline';socket?.close();render()});
 setInterval(()=>{const last=Math.max(socketOpened||0,lastSocket?Date.parse(lastSocket):0);if(last&&Date.now()-last>45000&&socket?.readyState===WebSocket.OPEN)socket.close();if(data)render()},10000);refresh();connect();
}
