import Panzoom from './vendor/panzoom-4.6.2.es.js';

const sheets = [
  {title:'플랫폼 아키텍처',file:'architecture-original.jpg',width:3800,height:2980,alt:'배규태가 공유한 플랫폼 아키텍처 원본. AWS Source, Platform, 온프레미스 Target, AI 제안과 리허설, DB 복제와 Edge 제어의 관계.'},
  {title:'S1–S4 이전 순서',file:'migration-original.jpg',width:3200,height:4408,alt:'배규태가 공유한 이전 순서 원본. S1 수집과 비교, S2 격리 리허설, S3 트래픽과 DB 전환, S4 데이터 복귀.'}
];
const root=document.getElementById('architecture-view');
if(root){
 root.innerHTML=[
  '<div class="view-heading"><div><div class="eyebrow">SOVA / 목적·구조</div><h1>플랫폼 구조와 이전 흐름</h1><p>배규태가 공유한 원본 도면입니다. 확대하고, 종이를 끌듯 이동하며 살펴보세요.</p></div><a class="paper-source" href="https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793878676334889" target="_blank" rel="noreferrer">11월 5일 원문 ↗</a></div>',
  '<div class="paper-reference">설계 참고 · 실제 클러스터 관측은 <a href="#view/live">네트워크 현황</a>에서 확인합니다.</div>',
  '<section class="paper-viewer" id="paper-viewer" aria-label="원본 구조도 확대 보기">',
  '<div class="paper-toolbar"><div class="paper-tabs" role="group" aria-label="원본 도면 선택"><button data-sheet="0" class="selected" aria-pressed="true">아키텍처</button><button data-sheet="1" aria-pressed="false">S1–S4 이전 순서</button></div>',
  '<div class="paper-controls"><button id="paper-out" aria-label="축소">−</button><output id="paper-scale" aria-live="polite">불러오는 중</output><button id="paper-in" aria-label="확대">＋</button><span class="paper-divider"></span><button id="paper-fit">화면 맞춤</button><button id="paper-original">100%</button><button id="paper-fullscreen" aria-label="도면 전체 화면">전체 화면</button><a id="paper-download" download>원본 ↓</a></div></div>',
  '<div class="paper-stage" id="paper-stage" role="region" tabindex="0" aria-label="도면. 드래그로 이동, 휠이나 핀치로 확대, 방향키로 이동, 0키로 화면 맞춤." aria-describedby="paper-help"><img id="paper-image" draggable="false" decoding="async"><p id="paper-error" hidden>도면을 불러오지 못했습니다. 원본 링크로 다시 열어 주세요.</p></div>',
  '<div class="paper-caption"><span id="paper-title"></span><span id="paper-help">드래그로 이동 · 휠/핀치로 확대 · 방향키 이동 · 0 화면 맞춤</span><a id="paper-open" target="_blank" rel="noreferrer">원본 크게 열기 ↗</a></div></section>',
  '<section class="paper-contract"><h2>원문과 현재 계약의 차이</h2><ul><li>T38 제품 경로는 현재 1회차만 지원합니다. 원격 감사 상태 <code>UNAVAILABLE</code>도 그대로 유지합니다.</li><li>T7 후보 빌드·스캔 → T15 동일 digest 시험·UID 정리 → 사람 승인 → T31 동일 digest 배포 순서입니다. 승인 뒤 재빌드하지 않습니다.</li><li>Edge 100% 전환과 DB 쓰기 전환은 별개입니다. 복귀는 Executor의 확정 <code>DONE</code>과 같은 실행의 무결성 확인 뒤 Edge를 Source로 돌립니다.</li></ul><p>원본 그림은 편집하지 않았습니다. 미병합 AI 후보 입력·발행, OpenBao 러너 신원, 동일 실행의 제품 인수와 실제 컷오버·복귀는 별도 확인 대상입니다.</p><a href="https://github.com/Softbank-Hackathon-Finals/platform/blob/develop/docs/contracts/T10-failback-v1-proposal.md" target="_blank" rel="noreferrer">현재 전환·복귀 계약 ↗</a></section>'
 ].join('');
 const stage=root.querySelector('#paper-stage'),image=root.querySelector('#paper-image'),viewer=root.querySelector('#paper-viewer'),scale=root.querySelector('#paper-scale');
 const motion=matchMedia('(prefers-reduced-motion: reduce)');
 let panzoom=null,selected=0,fitted=true,frame=0;
 function fit(){
  if(!panzoom||root.hidden||!stage.clientWidth)return;
  fitted=true;
  const value=Math.min((stage.clientWidth-32)/sheets[selected].width,(stage.clientHeight-32)/sheets[selected].height,1);
  panzoom.zoom(value,{animate:false});
  cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>panzoom?.pan(0,0,{animate:false}));
 }
 function show(index){
  selected=index;panzoom?.destroy();panzoom=null;fitted=true;
  image.style.transform='';image.style.visibility='hidden';
  const s=sheets[index];image.width=s.width;image.height=s.height;image.alt=s.alt;
  root.querySelector('#paper-title').textContent=s.title+' · '+s.width+' × '+s.height+' · 원본';
  for(const id of ['paper-open','paper-download'])root.querySelector('#'+id).href=s.file;
  root.querySelectorAll('[data-sheet]').forEach(b=>{b.classList.toggle('selected',+b.dataset.sheet===index);b.setAttribute('aria-pressed',String(+b.dataset.sheet===index))});
  root.querySelector('#paper-error').hidden=true;scale.textContent='불러오는 중';
  image.src=s.file;
 }
 image.addEventListener('load',()=>{
  panzoom=Panzoom(image,{canvas:true,minScale:.025,maxScale:3,startScale:.1,cursor:'grab',touchAction:'none',pinchAndPan:true,animate:false,step:.25});
  image.style.visibility='visible';fit();
 });
 image.addEventListener('error',()=>{root.querySelector('#paper-error').hidden=false;scale.textContent='조회 실패'});
 image.addEventListener('panzoomchange',event=>{scale.textContent=Math.round(event.detail.scale*100)+'%'});
 image.addEventListener('panzoomstart',()=>{fitted=false;stage.classList.add('dragging');stage.focus({preventScroll:true})});
 image.addEventListener('panzoomend',()=>stage.classList.remove('dragging'));
 stage.addEventListener('wheel',event=>{if(!panzoom)return;fitted=false;panzoom.zoomWithWheel(event)},{passive:false});
 root.querySelectorAll('[data-sheet]').forEach(b=>b.addEventListener('click',()=>{if(selected!==+b.dataset.sheet)show(+b.dataset.sheet)}));
 root.querySelector('#paper-in').addEventListener('click',()=>{fitted=false;panzoom?.zoomIn({animate:!motion.matches})});
 root.querySelector('#paper-out').addEventListener('click',()=>{fitted=false;panzoom?.zoomOut({animate:!motion.matches})});
 root.querySelector('#paper-fit').addEventListener('click',fit);
 root.querySelector('#paper-original').addEventListener('click',()=>{fitted=false;panzoom?.zoom(1,{animate:!motion.matches})});
 stage.addEventListener('keydown',event=>{
  if(!panzoom)return;const key=event.key;
  if(['+','=','-','0','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(key))event.preventDefault();else return;
  if(key==='0'){fit();return}fitted=false;
  if(key==='+'||key==='=')panzoom.zoomIn();else if(key==='-')panzoom.zoomOut();
  else {const distance=60/panzoom.getScale();panzoom.pan(key==='ArrowLeft'?distance:key==='ArrowRight'?-distance:0,key==='ArrowUp'?distance:key==='ArrowDown'?-distance:0,{relative:true})}
 });
 root.querySelector('#paper-fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement===viewer)await document.exitFullscreen();else await viewer.requestFullscreen()}catch{root.querySelector('#paper-open').focus()}});
 document.addEventListener('fullscreenchange',()=>{root.querySelector('#paper-fullscreen').textContent=document.fullscreenElement===viewer?'전체 화면 닫기':'전체 화면';if(fitted)fit()});
 new ResizeObserver(()=>{if(fitted)fit()}).observe(stage);
 new MutationObserver(()=>{if(!root.hidden&&fitted)fit()}).observe(root,{attributes:true,attributeFilter:['hidden']});
 show(0);
}
