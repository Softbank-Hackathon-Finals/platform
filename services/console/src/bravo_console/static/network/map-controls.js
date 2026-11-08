import Panzoom from './vendor/panzoom-4.6.2.es.js';

export function mountMapControls(){
 const stage=document.getElementById('atlas-viewport'),paper=document.getElementById('atlas-paper');
 if(!stage||!paper)return;
 const output=document.getElementById('atlas-scale'),view=document.getElementById('live-view');
 const motion=matchMedia('(prefers-reduced-motion: reduce)'),pointers=new Map();
 let fitted=true,moved=false,frame=0;
 const panzoom=Panzoom(paper,{canvas:true,minScale:.15,maxScale:3,step:.25,cursor:'grab',pinchAndPan:true,animate:false});
 function fit(){
  if(view.hidden||!stage.clientWidth)return;
  fitted=true;panzoom.zoom(Math.min((stage.clientWidth-32)/1000,(stage.clientHeight-32)/540,1),{animate:false});
  cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>panzoom.pan(0,0,{animate:false}));
 }
 stage.addEventListener('pointerdown',event=>{
  if(!pointers.size)moved=false;
  pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
  if(pointers.size>1){moved=true;fitted=false}
  stage.dataset.gesture='active';
 },true);
 document.addEventListener('pointermove',event=>{
  const start=pointers.get(event.pointerId);if(!start)return;
  if(Math.hypot(event.clientX-start.x,event.clientY-start.y)>6){moved=true;fitted=false;stage.classList.add('dragging')}
 },true);
 function end(event){
  pointers.delete(event.pointerId);
  if(!pointers.size){delete stage.dataset.gesture;stage.classList.remove('dragging')}
 }
 document.addEventListener('pointerup',end,true);document.addEventListener('pointercancel',end,true);
 // A drag may finish over a different node. Suppress only that pointer click;
 // a fresh pointerdown or a keyboard activation still selects normally.
 stage.addEventListener('click',event=>{if(moved&&event.detail!==0){event.preventDefault();event.stopImmediatePropagation()}},true);
 paper.addEventListener('panzoomchange',event=>{output.textContent=Math.round(event.detail.scale*100)+'%'});
 stage.addEventListener('wheel',event=>{fitted=false;panzoom.zoomWithWheel(event)},{passive:false});
 document.getElementById('atlas-zoom-in').addEventListener('click',()=>{fitted=false;panzoom.zoomIn({animate:!motion.matches})});
 document.getElementById('atlas-zoom-out').addEventListener('click',()=>{fitted=false;panzoom.zoomOut({animate:!motion.matches})});
 document.getElementById('atlas-fit').addEventListener('click',fit);
 document.getElementById('atlas-original').addEventListener('click',()=>{fitted=false;panzoom.zoom(1,{animate:!motion.matches})});
 stage.addEventListener('keydown',event=>{
  if(event.target!==stage||event.ctrlKey||event.metaKey||event.altKey)return;
  const key=event.key;if(!['+','=','-','0','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(key))return;
  event.preventDefault();if(key==='0'){fit();return}fitted=false;
  if(key==='+'||key==='=')panzoom.zoomIn();else if(key==='-')panzoom.zoomOut();
  else{const d=60/panzoom.getScale();panzoom.pan(key==='ArrowLeft'?d:key==='ArrowRight'?-d:0,key==='ArrowUp'?d:key==='ArrowDown'?-d:0,{relative:true})}
 });
 new ResizeObserver(()=>{if(fitted)fit()}).observe(stage);
 new MutationObserver(()=>{if(!view.hidden&&fitted)fit()}).observe(view,{attributes:true,attributeFilter:['hidden']});
 fit();
}

export function mountLiveWorkspace(){
 mountDetailWorkspaces();
 const workbench=document.querySelector('.atlas-workbench'),stage=workbench.querySelector('.atlas-stage');
 const map=document.createElement('section');map.id='live-map-panel';map.className='live-panel';
 while(stage.firstChild)map.append(stage.firstChild);stage.append(map);
 const routeHeading=document.getElementById('transport-count').closest('.resource-heading');
 const resourceHeading=document.getElementById('resource-count').closest('.resource-heading');
 const panels=[['map',map],['routes',routeHeading,document.querySelector('.transport-scroll')],['resources',resourceHeading,document.querySelector('.resource-scroll')],['health',document.querySelector('.diagnostic-fold'),document.getElementById('live-gaps')]];
 for(const [name,...children] of panels){
  let panel=children[0];if(name!=='map'){panel=document.createElement('section');panel.id='live-'+name+'-panel';panel.className='live-panel';panel.append(...children);stage.append(panel)}
  panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby','live-tab-'+name);panel.hidden=name!=='map';
 }
 const tabs=document.createElement('div');tabs.className='live-workspace-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','네트워크 작업 영역');
 tabs.innerHTML=[['map','지도'],['routes','전송 경로'],['resources','리소스'],['health','수집 상태']].map(([id,label])=>`<button id="live-tab-${id}" role="tab" data-live-panel="${id}" aria-controls="live-${id}-panel" aria-selected="${id==='map'}" tabindex="${id==='map'?0:-1}">${label}</button>`).join('');
 workbench.before(tabs);
 let layoutFrame=0;
 function resizeWorkspace(){
  cancelAnimationFrame(layoutFrame);layoutFrame=requestAnimationFrame(()=>{
   if(document.getElementById('live-view').hidden)return;
   const min=innerWidth<=700?470:320;
   const height=Math.max(min,innerHeight-workbench.getBoundingClientRect().top-16);
   if(workbench.style.height!==height+'px')workbench.style.height=height+'px';
  });
 }
 addEventListener('resize',resizeWorkspace);
 new ResizeObserver(resizeWorkspace).observe(document.getElementById('live-summary'));
 new MutationObserver(resizeWorkspace).observe(document.getElementById('live-view'),{attributes:true,attributeFilter:['hidden'],childList:true});
 resizeWorkspace();
 function show(name){
  for(const [id] of panels)document.getElementById('live-'+id+'-panel').hidden=id!==name;
  tabs.querySelectorAll('button').forEach(b=>{const chosen=b.dataset.livePanel===name;b.setAttribute('aria-selected',String(chosen));b.tabIndex=chosen?0:-1});
  workbench.dataset.panel=name;
 }
 window.addEventListener('atlas-inspect',event=>show(event.detail.panel));
 tabs.addEventListener('click',event=>{const b=event.target.closest('[data-live-panel]');if(b)show(b.dataset.livePanel)});
 tabs.addEventListener('keydown',event=>{const items=[...tabs.querySelectorAll('button')],i=items.indexOf(document.activeElement);if(i<0)return;let next;if(event.key==='ArrowRight')next=(i+1)%items.length;else if(event.key==='ArrowLeft')next=(i+items.length-1)%items.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=items.length-1;else return;event.preventDefault();show(items[next].dataset.livePanel);items[next].focus()});
 document.addEventListener('click',event=>{const b=event.target.closest('[data-open-live-panel]');if(b)show(b.dataset.openLivePanel)});
}

function mountDetailWorkspaces(){
 const configs=[
  ['l3',[
   ['routes','주소 · 다음 홉 · 주소 변환',['.resource-heading','.table-scroll']],
   ['layers','계층 개요',['.l3-route-strip','#l3-stack']],
   ['scope','범위와 해석',['.scope-note']]
  ]],
  ['permissions',[
   ['matrix','권한 매트릭스',['.table-scroll']],
   ['gates','접근 경계',['.gate-strip','.protocol-primer']]
  ]],
  ['evidence',[
   ['records','직접 조회와 검증 기록',['.section-title:first-of-type','#evidence-list']],
   ['pending','최초 제작 당시 남은 연결',['.section-title:last-of-type','#pending-list']],
   ['scope','범위와 해석',['.scope-note']]
  ]]
 ];
 for(const [name,items] of configs){
  const view=document.getElementById(name+'-view');if(!view)continue;
  const nav=document.createElement('div');nav.className='detail-tabs';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','상세 보기');
  const surface=document.createElement('div');surface.className='detail-surface';
  for(const [id,label,selectors] of items){
   const panel=document.createElement('section');panel.id=name+'-panel-'+id;panel.className='detail-panel';panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',name+'-tab-'+id);
   for(const selector of selectors){const child=view.querySelector(selector);if(child)panel.append(child)}
   surface.append(panel);
   const button=document.createElement('button');button.id=name+'-tab-'+id;button.dataset.panel=id;button.textContent=label;button.setAttribute('role','tab');button.setAttribute('aria-controls',panel.id);nav.append(button);
  }
  view.append(nav,surface);view.classList.add('detail-workspace');
  function select(id){for(const button of nav.children){const active=button.dataset.panel===id;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;document.getElementById(button.getAttribute('aria-controls')).hidden=!active}}
  nav.addEventListener('click',event=>{const b=event.target.closest('[data-panel]');if(b)select(b.dataset.panel)});
  nav.addEventListener('keydown',event=>{const buttons=[...nav.children],i=buttons.indexOf(document.activeElement);if(i<0)return;const next=event.key==='ArrowRight'?(i+1)%buttons.length:event.key==='ArrowLeft'?(i+buttons.length-1)%buttons.length:event.key==='Home'?0:event.key==='End'?buttons.length-1:-1;if(next<0)return;event.preventDefault();select(buttons[next].dataset.panel);buttons[next].focus()});
  if(name==='l3')document.addEventListener('click',event=>{if(event.target.closest('#inspect-l3'))select('routes')});
  if(name==='evidence')document.addEventListener('click',event=>{if(event.target.closest('[data-source]'))select('records')},true);
  let frame;function resize(){cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{if(!view.hidden)surface.style.height=Math.max(260,innerHeight-surface.getBoundingClientRect().top-18)+'px'})}
  addEventListener('resize',resize);addEventListener('atlas-locale',resize);new MutationObserver(resize).observe(view,{attributes:true,attributeFilter:['hidden']});
  select(items[0][0]);resize();
 }
}
