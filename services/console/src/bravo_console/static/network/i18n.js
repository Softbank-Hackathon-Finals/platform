import {phrases,patterns} from './locales.js';

export const supportedLocales=['ko','en','ja'];
const preferenceKey='bravo-atlas-language';
export function browserLocale(languages=[]){for(const code of languages){const base=String(code).toLowerCase().split('-')[0];if(supportedLocales.includes(base))return base}return 'en'}
export function chooseLocale({preference,country,languages=[]}={}){if(supportedLocales.includes(preference))return preference;if(country==='KR')return 'ko';if(country==='JP')return 'ja';if(typeof country==='string'&&/^[A-Z]{2}$/.test(country)&&!['XX','T1'].includes(country))return 'en';return browserLocale(languages)}
export function countryFromTrace(trace){const values=String(trace).split(/\r?\n/).filter(line=>line.startsWith('loc=')).map(line=>line.slice(4));return values.length===1&&/^[A-Z]{2}$/.test(values[0])&&values[0]!=='XX'?values[0]:null}
let locale=typeof navigator!=='undefined'?browserLocale(navigator.languages||[navigator.language]):'en';
const exact=new Map(phrases.map(([ko,en,ja])=>[ko,{en,ja}]));
const escapePattern=value=>value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const fragments=[...exact.keys()].sort((a,b)=>b.length-a.length).map(key=>({key,pattern:new RegExp('(?<![가-힣])'+escapePattern(key)+'(?![가-힣])','g')}));
export const getLocale=()=>locale;
export function t(value,language=locale){
 const source=String(value??'');if(language==='ko'||!/[가-힣]/.test(source))return source;
 const lead=source.match(/^\s*/)[0],tail=source.match(/\s*$/)[0],trim=source.trim();
 if(exact.has(trim))return lead+exact.get(trim)[language]+tail;
 const counts=patterns.filter(([re])=>re.source==='(\\d+)개');
 let prepared=source;for(const [re,en,ja] of patterns){if(re.source!=='(\\d+)개')prepared=prepared.replace(re,language==='ja'?ja:en)}
 // Phrase matching is only for application-authored prose. Resource identifiers are never changed.
 let out=prepared;for(const {key,pattern} of fragments){if(out.includes(key))out=out.replace(pattern,()=>exact.get(key)[language])}
 for(const [re,en,ja] of counts)out=out.replace(re,language==='ja'?ja:en);
 // Unknown prose stays intact; never produce a mixed sentence by translating syllables.
 return /[가-힣]/.test(out)?source:out;
}
const originals=new WeakMap(),attributeOriginals=new WeakMap();
let observer,root,globe,menu,preferred=null,automatic=locale,automaticSource='browser',scheduled=false;
const skip='script,style,noscript,textarea,pre,[data-no-translate],.probe-evidence,.probe-receipt code';
function translateNode(node){
 if(!node.parentElement||node.parentElement.closest(skip))return;
 const old=originals.get(node),current=node.nodeValue;
 const original=old&&current===old.rendered?old.original:current;
 const rendered=t(original);originals.set(node,{original,rendered});if(rendered!==current)node.nodeValue=rendered;
}
function translateAttributes(el){
 if(el.closest(skip))return;
 const saved=attributeOriginals.get(el)||{};
 for(const name of ['aria-label','title','placeholder','alt']){if(!el.hasAttribute(name))continue;const current=el.getAttribute(name),old=saved[name],original=old&&current===old.rendered?old.original:current,rendered=t(original);saved[name]={original,rendered};if(rendered!==current)el.setAttribute(name,rendered)}attributeOriginals.set(el,saved);
}
function observe(){observer?.observe(root,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['aria-label','title','placeholder','alt']})}
export function translatePage(){
 if(!root)return;observer?.disconnect();
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);while(walker.nextNode())translateNode(walker.currentNode);
 root.querySelectorAll('[aria-label],[title],[placeholder],[alt]').forEach(translateAttributes);
 document.documentElement.lang=locale;document.documentElement.dataset.locale=locale;updateMenu();observe();
}
function schedule(){if(scheduled)return;scheduled=true;queueMicrotask(()=>{scheduled=false;translatePage()})}
export function setLocale(next,{persist=true}={}){
 if(!supportedLocales.includes(next))return;locale=next;
 if(persist){preferred=next;try{localStorage.setItem(preferenceKey,next)}catch{}}
 translatePage();if(typeof window!=='undefined')window.dispatchEvent(new CustomEvent('atlas-locale',{detail:{locale,automatic:!preferred}}));
}
function updateMenu(){
 if(!globe)return;const names={ko:'한국어',en:'English',ja:'日本語'},labels={ko:'언어 선택',en:'Select language',ja:'言語を選択'};
 globe.querySelector('span').textContent=locale.toUpperCase();globe.setAttribute('aria-label',labels[locale]+': '+names[locale]);
 menu.querySelectorAll('[data-locale]').forEach(button=>{button.setAttribute('aria-checked',String(preferred===button.dataset.locale));button.classList.toggle('selected',preferred===button.dataset.locale)});
 const auto=menu.querySelector('[data-locale-auto]');auto.setAttribute('aria-checked',String(!preferred));auto.classList.toggle('selected',!preferred);auto.querySelector('span').textContent={ko:'자동',en:'Automatic',ja:'自動'}[locale];
 auto.querySelector('small').textContent=({ko:{country:'접속 국가',browser:'브라우저 언어'},en:{country:'Access country',browser:'Browser language'},ja:{country:'接続元の国',browser:'ブラウザーの言語'}})[locale][automaticSource]+' · '+names[automatic];
}
function closeMenu(focus=false){menu.hidden=true;globe.setAttribute('aria-expanded','false');if(focus)globe.focus()}
function mountMenu(){
 const topbar=document.querySelector('.topbar');if(!topbar)return;
 const holder=document.createElement('div');holder.className='locale-control';holder.dataset.noTranslate='';
 holder.innerHTML='<button id="locale-toggle" class="locale-toggle" aria-haspopup="menu" aria-expanded="false" aria-controls="locale-menu"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 4 6 4 9s-1 6-4 9c-3-3-4-6-4-9s1-6 4-9Z"/></svg><span></span></button><div id="locale-menu" class="locale-menu" role="menu" hidden><button role="menuitemradio" data-locale="ko" lang="ko">한국어</button><button role="menuitemradio" data-locale="en" lang="en">English</button><button role="menuitemradio" data-locale="ja" lang="ja">日本語</button><button role="menuitemradio" data-locale-auto><span></span><small></small></button></div>';
 topbar.insertBefore(holder,document.getElementById('help-btn'));globe=holder.querySelector('#locale-toggle');menu=holder.querySelector('#locale-menu');
 globe.addEventListener('click',()=>{menu.hidden=!menu.hidden;globe.setAttribute('aria-expanded',String(!menu.hidden));if(!menu.hidden)menu.querySelector('[aria-checked="true"]')?.focus()});
 globe.addEventListener('keydown',event=>{if(event.key==='ArrowDown'){event.preventDefault();menu.hidden=false;globe.setAttribute('aria-expanded','true');menu.querySelector('button').focus()}});
 menu.querySelectorAll('[data-locale]').forEach(button=>button.addEventListener('click',()=>{setLocale(button.dataset.locale);closeMenu(true)}));
 menu.querySelector('[data-locale-auto]').addEventListener('click',()=>{preferred=null;try{localStorage.removeItem(preferenceKey)}catch{}setLocale(automatic,{persist:false});closeMenu(true)});
 menu.addEventListener('keydown',event=>{const buttons=[...menu.querySelectorAll('button')],at=buttons.indexOf(document.activeElement);if(event.key==='Escape'){event.preventDefault();closeMenu(true)}else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(at+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;buttons[next].focus()}});
 document.addEventListener('pointerdown',event=>{if(!holder.contains(event.target))closeMenu()});
 // Pointer focus can transiently report no destination before the menu item click.
 holder.addEventListener('focusout',event=>{if(event.relatedTarget&&!holder.contains(event.relatedTarget))closeMenu()});
 updateMenu();
}
async function initialize(){
 try{const stored=localStorage.getItem(preferenceKey);if(supportedLocales.includes(stored))preferred=stored}catch{}
 automatic=browserLocale(navigator.languages||[navigator.language]);locale=preferred||automatic;
 root=document.body;mountMenu();observer=new MutationObserver(schedule);translatePage();
 const country=null;
 automatic=chooseLocale({country,languages:navigator.languages||[navigator.language]});automaticSource=country?'country':'browser';if(!preferred)setLocale(automatic,{persist:false});else updateMenu();
}
if(typeof document!=='undefined'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initialize,{once:true});else initialize()}
