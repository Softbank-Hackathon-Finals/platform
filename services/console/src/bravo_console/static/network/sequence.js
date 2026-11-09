const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// These are explanatory lanes for the dated path descriptions, not observed messages.
// A self-step or note is used whenever the source does not establish a network call.
const layouts = {
 'human-target': {lanes:[['운영자 기기','Personal identity'],['기존 공유망','Tailscale policy'],['Target API','TLS · Kubernetes RBAC']], messages:['Tailscale login','Policy · TCP 443','HTTPS GET /pods','GET / LIST · RBAC'], arrows:[[0,1],[1,2],[1,2],[2,2]]},
 'human-platform': {lanes:[['운영자 기기','Personal identity'],['API 프록시','Tailscale identity'],['Platform API','GET / LIST · RBAC']], messages:['TLS · TCP 443','User impersonation','GET / LIST'], arrows:[[0,1],[1,2],[2,2]]},
 'source-access': {lanes:[['AWS 신원','Profile / SSO'],['사설 경로','SSM / VPC'],['Source EKS','Token · access entry']], messages:['Profile / SSO','SSM / VPC','EKS token','Session close'], arrows:[[0,0],[0,1],[0,2],null]},
 rehearsal: {lanes:[['Rehearsal Job','ServiceAccount'],['전용 Egress','Service / ProxyGroup'],['Target API','TLS · RBAC']], messages:['Job + SA','Service / ProxyGroup','TLS server-name','ConfigMap · RBAC','Probe cleanup'], arrows:[[0,0],[0,1],[1,2],[2,2],null]},
 apply: {lanes:[['Apply Job','ServiceAccount'],['전용 Egress','Service / ProxyGroup'],['Target API','Named Deployment']], messages:['Job + SA','Role-specific egress','SA Authorization','GET / PATCH / GET','실제 앱 · 운영 issuer'], arrows:[[0,0],[0,1],[1,2],[2,2],null]},
 replication: {lanes:[['Source DB','PostgreSQL / slot'],['DB 오버레이','TCP 5432'],['Target DB','Replication role']], messages:['Source / slot','PostgreSQL · TCP 5432','timeout / forward OFF','복구 검증 · 실행 창'], arrows:[[0,0],[0,2],[2,2],null]},
 storage: {lanes:[['허용된 Pod','NetworkPolicy'],['ClusterIP','Service / port'],['DB / S3','Data credentials']], messages:['NetworkPolicy','DNS / ClusterIP','DB role / S3 credential'], arrows:[[0,0],[0,1],[1,2]]},
 'platform-control': {lanes:[['Window intake','30981 → 8080'],['State DB','PostgreSQL · 5432'],['Executor','HTTP · 8080'],['외부 API / DB','443 · 5432 · 6432']], messages:['Service · 30981 → 8080','PostgreSQL · 5432','Executor · 8080','Egress · 443 / 5432 / 6432'], arrows:[[0,0],[1,1],[2,2],null]}
};
export function sequenceModel(path) {
 const layout = layouts[path.id];
 if(!layout) return null;
 return {...layout,steps:path.steps.map(([phase,title,description],index)=>({phase,title,description,message:layout.messages[index],arrow:layout.arrows[index]??null,index}))};
}

export function createSequenceExplorer(root,{paths,onSelectPath}) {
 let path=null,model=null,current=0,timer=null,playing=false,mobilePanel='flow';
 const pause=()=>{clearInterval(timer);timer=null;playing=false;updateControls()};
 const overview=()=>{
  root.innerHTML=`<div class="sequence-empty"><span class="eyebrow">SEQUENCE / PATH</span><h2>살펴볼 경로를 선택하세요.</h2><p>선택한 경로의 참여자와 순서만 표시합니다.</p><div class="sequence-path-picker">${paths.filter(p=>p.id!=='overview').map(p=>`<button data-sequence-path="${p.id}"><span>${p.num}</span><strong>${escape(p.name)}</strong><small>${escape(p.protocol)}</small><b aria-hidden="true">→</b></button>`).join('')}</div></div>`;
  root.querySelectorAll('[data-sequence-path]').forEach(b=>b.addEventListener('click',()=>onSelectPath(b.dataset.sequencePath)));
 };
 function updateControls(){
  const play=root.querySelector('[data-seq-action="play"]');if(!play)return;
  play.textContent=playing?'일시정지':'재생';play.setAttribute('aria-label',playing?'시퀀스 일시정지':'시퀀스 재생');
  root.querySelector('[data-seq-action="previous"]').disabled=current===0;
  root.querySelector('[data-seq-action="next"]').disabled=current===model.steps.length-1;
  root.querySelector('.sequence-position').textContent=String(current+1).padStart(2,'0')+' / '+String(model.steps.length).padStart(2,'0');
 }
 function select(index,{manual=true}={}) {
  if(!model)return;
  if(manual)pause();
  current=Math.max(0,Math.min(model.steps.length-1,index));
  root.querySelectorAll('[data-sequence-step]').forEach(el=>{const i=Number(el.dataset.sequenceStep);el.classList.toggle('current',i===current);el.classList.toggle('visited',i<current);el.setAttribute('aria-current',i===current?'step':'false')});
  const step=model.steps[current];
  root.querySelector('.sequence-detail').innerHTML=`<div class="sequence-detail-head"><span class="eyebrow">STEP ${String(current+1).padStart(2,'0')} / ${escape(step.phase)}</span><h2>${escape(step.title)}</h2></div><p class="sequence-description">${escape(step.description)}</p><div class="sequence-boundary"><span>해석 범위</span><p>구성·검증 기록의 설명 순서입니다. 실시간 패킷이나 실행 성공을 뜻하지 않습니다.</p></div><details class="sequence-permissions"><summary>이 경로의 허용·제한</summary><div>${path.allow.map(t=>`<p class="seq-allow">${escape(t)}</p>`).join('')}${path.deny.map(t=>`<p class="seq-deny">${escape(t)}</p>`).join('')}</div></details>`;
  updateControls();
 }
 function togglePlay(){
  if(playing){pause();return}
  if(current===model.steps.length-1)select(0);
  playing=true;updateControls();
  timer=setInterval(()=>{if(current>=model.steps.length-1){pause();return}select(current+1,{manual:false});if(current===model.steps.length-1)pause()},2200);
 }
 function draw(){
  if(!model){overview();return}
  const width=model.lanes.length*206+46,height=130+model.steps.length*66,x=i=>126+i*206;
  root.innerHTML=`<div class="sequence-context"><div><span class="eyebrow">${escape(path.protocol)} / ${model.lanes.length} <span>참여자</span></span><strong>${escape(path.name)}</strong></div><span class="sequence-record-label">구성 기록 · 실시간 추적 아님</span></div><div class="sequence-mobile-tabs" role="group" aria-label="시퀀스 표시"><button data-seq-panel="flow" class="selected">흐름</button><button data-seq-panel="detail">단계 상세</button></div><div class="sequence-workspace" data-mobile-panel="${mobilePanel}"><div class="sequence-diagram-scroll" tabindex="0" role="region" aria-label="선택 경로의 시퀀스. 좌우 방향키로 단계를 이동합니다."><svg class="sequence-diagram" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="group" aria-label="${escape(path.name)} 구성 시퀀스"><defs><marker id="sequence-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z"/></marker></defs>${model.lanes.map(([title,subtitle],i)=>`<g class="sequence-participant"><rect x="${x(i)-86}" y="15" width="172" height="59" rx="7"/><foreignObject x="${x(i)-80}" y="24" width="160" height="24"><div xmlns="http://www.w3.org/1999/xhtml" class="sequence-participant-name">${escape(title)}</div></foreignObject><text x="${x(i)}" y="61" class="sequence-participant-sub">${escape(subtitle)}</text><path class="sequence-lifeline" d="M${x(i)} 74V${height-16}"/></g>`).join('')}${model.steps.map(step=>{
   const y=122+step.index*66,arrow=step.arrow,a=arrow?x(arrow[0]):0,b=arrow?x(arrow[1]):0;
   const d=arrow?(a===b?`M${a} ${y}h56v22h-56`:`M${a} ${y}H${b}`):'';
   return `<g class="sequence-message" data-sequence-step="${step.index}" role="button" tabindex="0" aria-label="${step.index+1}. ${escape(step.title)}"><rect class="sequence-row-hit" x="14" y="${y-28}" width="${width-28}" height="58" rx="6"/><text class="sequence-step-number" x="26" y="${y+4}">${String(step.index+1).padStart(2,'0')}</text>${arrow?`<path class="sequence-message-line" d="${d}" marker-end="url(#sequence-arrow)"/><rect class="sequence-activation" x="${b-3}" y="${y-6}" width="6" height="18" rx="2"/>`:`<rect class="sequence-note-box" x="86" y="${y-10}" width="${width-126}" height="30" rx="5"/>`}<foreignObject x="${arrow?(a===b?a-70:Math.min(a,b)+10):96}" y="${arrow?y-27:y-5}" width="${arrow?(a===b?140:Math.abs(b-a)-20):width-146}" height="22"><div xmlns="http://www.w3.org/1999/xhtml" class="sequence-message-label">${escape(step.message)}</div></foreignObject></g>`
  }).join('')}</svg></div><aside class="sequence-detail" aria-live="polite" aria-label="현재 단계 상세"></aside></div><div class="sequence-controls"><div class="sequence-progress" role="group" aria-label="단계 선택">${model.steps.map((s,i)=>`<button data-sequence-step="${i}" aria-label="${i+1}. ${escape(s.title)}">${String(i+1).padStart(2,'0')}</button>`).join('')}</div><output class="sequence-position"></output><div class="sequence-playback"><button data-seq-action="restart" aria-label="처음 단계로">처음</button><button data-seq-action="previous" aria-label="이전 단계">이전</button><button data-seq-action="play" class="sequence-play">재생</button><button data-seq-action="next" aria-label="다음 단계">다음</button></div></div>`;
  root.querySelectorAll('[data-sequence-step]').forEach(el=>{
   const choose=()=>select(Number(el.dataset.sequenceStep));el.addEventListener('click',choose);
   if(el.tagName.toLowerCase()==='g')el.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose()}});
  });
  root.querySelectorAll('[data-seq-action]').forEach(b=>b.addEventListener('click',()=>{const action=b.dataset.seqAction;if(action==='play')togglePlay();else select(action==='restart'?0:current+(action==='previous'?-1:1))}));
  root.querySelector('.sequence-diagram-scroll').addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();select(current+(e.key==='ArrowRight'?1:-1))}});
  root.querySelectorAll('[data-seq-panel]').forEach(b=>b.addEventListener('click',()=>{mobilePanel=b.dataset.seqPanel;root.querySelector('.sequence-workspace').dataset.mobilePanel=mobilePanel;root.querySelectorAll('[data-seq-panel]').forEach(x=>x.classList.toggle('selected',x===b))}));
  select(current,{manual:false});
 }
 return {
  setPath(next){if(path?.id===next.id)return;pause();path=next;model=sequenceModel(path);current=0;mobilePanel='flow';draw()},
  pause,
  destroy(){pause();root.replaceChildren()}
 };
}
