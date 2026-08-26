
const $=s=>document.querySelector(s);
const api=(u,o)=>fetch(u,o).then(r=>r.json());
let STATE={series:[]};

function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}
function fmtDate(s){if(!s)return '--';const d=new Date(s+'T00:00:00');if(isNaN(d))return s;
  return d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});}
function isReleased(s){if(!s)return null;const d=new Date(s+'T00:00:00');if(isNaN(d))return null;const t=new Date();t.setHours(0,0,0,0);return d<t;}
function isTodayStr(sd){if(!sd)return false;const d=new Date(sd+'T00:00:00');if(isNaN(d))return false;const t=new Date();t.setHours(0,0,0,0);return d.getTime()===t.getTime();}
function storeLink(s,n,kind){                     // page URL for book n's edition, or null
  if(n==null) return null;
  const host=s.store_host; if(!host) return (s.url||null);
  const key=String(n);
  const asin=(kind==='audio'?(s.asins_audio||{})[key]:(s.asins_novel||{})[key])||(s.asins||{})[key];
  return asin?('https://'+host+'/dp/'+asin):(s.url||null);
}
function authorLink(s){
  if(!s.store_host||!s.author) return null;
  return 'https://'+s.store_host+'/s?k='+encodeURIComponent(s.author);
}
function wrapLink(html,url,title){ return url?`<a class="plain" href="${attrEsc(url)}" target="_blank" rel="noopener" ${title?`title="${attrEsc(title)}"`:''}>${html}</a>`:html; }
function latestEntry(s){let best=null,bn=-1;(s.timeline||[]).forEach(e=>{const n=(e&&e.n!=null)?+e.n:NaN;if(!isNaN(n)&&n>bn){bn=n;best=e;}});return best;}
function daysLate(iso){      // whole days since a date that has already passed
  const dt=new Date(iso+'T00:00:00');if(isNaN(dt))return 0;
  const t=new Date();t.setHours(0,0,0,0);
  return Math.max(0,Math.round((t-dt)/86400000));
}
function bkTag(n){ return n!=null?`<span class="bk">Book ${n}</span> `:''; }
// novel_n tracks the highest book number, which can come from an audio-only
// entry; the Novel column must name whichever book actually has this date.
function novelBookN(s){
  const hit=(s.timeline||[]).find(e=>e&&e.novel===s.novel_date&&e.n!=null);
  return hit?hit.n:(s.novel_n??null);
}
function audioFact(s){
  // The Audiobook column tracks whatever we actually know, in order of strength:
  // the tracked book's own date, then a confirmed pre-order for an earlier book
  // (the series-level field only ever mirrors the LATEST book, which is why this
  // read blank), then how far past due an overdue one is, as a red count. Each
  // names its book, because it often isn't the one on the Novel line.
  if(s.audiobook_date)
    return bkTag(s.novel_n)+fmtDate(s.audiobook_date)+dateTag(s.audiobook_date,(latestEntry(s)||{}).audio_pre===false);
  const ra=nextRealAudio(s);
  if(ra){
    const val=bkTag(ra.n)+fmtDate(ra.date)+dateTag(ra.date,ra.live);
    const t=ra.n!=null?`Book ${ra.n}'s audiobook`:'The next audiobook';
    return wrapLink(val,storeLink(s,ra.n,'audio'),t+' — open on Amazon');
  }
  if(s.predicted_date&&isReleased(s.predicted_date)===true){
    const n=daysLate(s.predicted_date);
    const who=s.predicted_book_n!=null?`Book ${s.predicted_book_n}'s audiobook is `:'Overdue by ';
    return bkTag(s.predicted_book_n)
      +`<span style="color:var(--rose)" title="${attrEsc(who+n+' day'+(n===1?'':'s')+' past its expected window (expected ~ '+fmtDate(s.predicted_date)+'). Later books cannot be predicted until it lands.')}">−${n}d</span>`;
  }
  return '—';
}
function nextRealAudio(s){   // soonest still-upcoming REAL audiobook date in the timeline, or null
  let best=null;
  (s.timeline||[]).forEach(e=>{
    if(!e||!e.audiobook||isReleased(e.audiobook)!==false) return;
    if(!best||e.audiobook<best.date) best={date:e.audiobook,n:e.n!=null?+e.n:null,live:e.audio_pre===false};
  });
  return best;
}
function dateTag(d,live){const past=isReleased(d);if(past===null)return '';
  const out=past||(live===true&&isTodayStr(d));   // confirmed buyable on release day counts as out
  const label=past?'out':(out?'out now':'pre-order');
  const css=out?'color:#56d18a;border-color:#2c5a3f;background:#16271d':'color:#e7b14c;border-color:#5a4a25;background:#262017';
  return `<span style="font:600 9px/1 var(--mono,monospace);letter-spacing:.04em;text-transform:uppercase;padding:1px 5px;border:1px solid;border-radius:6px;margin-left:6px;vertical-align:middle;${css}">${label}</span>`;}
function confChip(c){
  if(!c) return '';
  const M={high:['high','#56d18a','#16271d','#2c5a3f'],medium:['med','#f0a500','#262017','#5a4a25'],low:['low','#b9a6d6','#221d3a','#3a3360']};
  const m=M[c.level]||M.low;
  const pm=(c.spread_days!=null)?` ±${c.spread_days}d`:'';
  const drop=c.outliers?`, ignoring ${c.outliers} freak gap${c.outliers===1?'':'s'}`:'';
  const basis=(c.spread_days!=null)
    ? `${c.level} confidence — based on ${c.n} past gap${c.n===1?'':'s'} that varied by about ±${c.spread_days} days${drop}`
    : `${c.level} confidence — only ${c.n} past release to go on, so the spread is unknown${drop}`;
  return ` <span title="${esc(basis)}" style="font:600 9px/1 var(--mono,monospace);letter-spacing:.04em;text-transform:uppercase;padding:1px 5px;border:1px solid ${m[3]};background:${m[2]};color:${m[1]};border-radius:6px;vertical-align:middle">${m[0]}${pm}</span>`;}
function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML;}
function attrEsc(s){return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}

/* ---- timeline hover card: cover art + details for a release dot ---- */
let HOVERCARD=null,HIDE_T=null,OVER_CARD=false,OVER_MARK=false;
function hideHover(){clearTimeout(HIDE_T);OVER_CARD=OVER_MARK=false;if(HOVERCARD)HOVERCARD.style.display='none';}
function hideHoverSoon(){clearTimeout(HIDE_T);HIDE_T=setTimeout(()=>{if(!OVER_CARD&&!OVER_MARK)hideHover();},500);}
function showHover(mark){
  let tip;try{tip=JSON.parse(mark.dataset.tip||'null');}catch(_){tip=null;}
  if(!tip)return;
  // same dot already showing: don't rebuild (that flickered the card and swallowed Buy clicks)
  if(HOVERCARD&&HOVERCARD.style.display!=='none'&&HOVERCARD._tipKey===mark.dataset.tip)return;
  if(!HOVERCARD){HOVERCARD=document.createElement('div');HOVERCARD.className='hovercard';document.body.appendChild(HOVERCARD);
    HOVERCARD.onmouseenter=()=>{OVER_CARD=true;clearTimeout(HIDE_T);};
    HOVERCARD.onmouseleave=()=>{OVER_CARD=false;hideHoverSoon();};
    HOVERCARD.addEventListener('click',async ev=>{
      const b=ev.target.closest&&ev.target.closest('.hc-buy');if(!b)return;
      ev.preventDefault();ev.stopPropagation();
      b.disabled=true;b.textContent='Asking\u2026';
      try{const r=await api('/api/buy/for-book',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({series_id:b.dataset.sid,n:parseInt(b.dataset.n,10)})});
          toast(r.message||r.error||(r.ok?'Queued':'Failed'));}
      catch(e){toast('Failed: '+e);}
      hideHover();
    });}
  const name=tip.est?('Estimated audiobook'+(tip.estN!=null?` · book ${tip.estN}`:''))
    :(tip.t||((tip.sn||'')+(tip.nN!=null?' '+tip.nN:(tip.aN!=null?' '+tip.aN:''))));
  // a broken per-book cover URL degrades to the series cover, then to no image
  const img=(tip.cover&&!tip.est)?`<img src="${attrEsc(tip.cover)}" alt="" data-fb="${attrEsc(tip.cover2||'')}" onerror="if(this.dataset.fb){this.src=this.dataset.fb;this.dataset.fb='';}else{this.remove();}">`:'';
  const narr=tip.narr?`<div class="hc-n">Narrated by ${esc(tip.narr)}</div>`:'';
  const hint=(tip.est&&tip.estWhy)?`<div class="hc-n">${esc(tip.estWhy)}</div>`:'';
  const buy=tip.buyN!=null?`<button class="small hc-buy" data-sid="${attrEsc(tip.sid||'')}" data-n="${attrEsc(String(tip.buyN))}" title="Checks the live companion price, then asks you to approve \u2014 nothing is bought without your OK">Buy</button>`:'';
  HOVERCARD.innerHTML=`${img}<div><div class="hc-t">${esc(name)}</div><div class="hc-d">${esc(tip.label)}</div>${narr}${hint}${buy}</div>`;
  HOVERCARD._tipKey=mark.dataset.tip;
  HOVERCARD.style.display='flex';HOVERCARD.style.visibility='hidden';HOVERCARD.style.left='0px';HOVERCARD.style.top='0px';
  const r=mark.getBoundingClientRect(),hw=HOVERCARD.offsetWidth,hh=HOVERCARD.offsetHeight;
  let x=Math.min(Math.max(8,r.left+r.width/2-hw/2),window.innerWidth-hw-8);
  let y=r.top-hh-10; if(y<8)y=r.bottom+10;
  HOVERCARD.style.left=x+'px';HOVERCARD.style.top=y+'px';HOVERCARD.style.visibility='visible';
}
document.addEventListener('mouseover',e=>{
  const m=e.target.closest&&e.target.closest('.mark[data-tip]');
  if(m){OVER_MARK=true;clearTimeout(HIDE_T);showHover(m);return;}
  if(HOVERCARD&&!(e.target===HOVERCARD||HOVERCARD.contains(e.target))){OVER_MARK=false;hideHoverSoon();}
});
document.addEventListener('mouseout',e=>{const m=e.target.closest&&e.target.closest('.mark[data-tip]');if(m){OVER_MARK=false;hideHoverSoon();}});
window.addEventListener('scroll',()=>{if(!OVER_CARD)hideHover();},true);

let LAST_CHANGES=null;   // map id -> last_change, to spot new updates
function checkForNotifications(){
  const cur={};STATE.series.forEach(s=>cur[s.id]=s.last_change||'');
  if(LAST_CHANGES!==null && 'Notification' in window && Notification.permission==='granted'){
    STATE.series.forEach(s=>{
      const prev=LAST_CHANGES[s.id];
      if(s.last_change && prev!==undefined && s.last_change!==prev){
        try{new Notification('Release Radar — '+s.name,{body:s.last_change});}catch(_){}
      }
    });
  }
  LAST_CHANGES=cur;
}

async function refresh(){
  STATE=await api('/api/state');
  if(STATE.app_build){
    if(window.__appBuild&&window.__appBuild!==STATE.app_build){location.reload();return;}
    window.__appBuild=STATE.app_build;
  }
  const dot=$('#extDot');
  if(dot&&STATE.ext_seen_s!=null){
    const s2=STATE.ext_seen_s;
    dot.className='extdot '+(s2<120?'ok':(s2<21600?'warn':'bad'));
    dot.title=s2<120?('Extension: checking in ('+s2+'s ago)')
      :('Extension silent for '+(s2<7200?Math.round(s2/60)+' min':Math.round(s2/3600)+' h')+' — is Floorp open and the add-on enabled?');
  }
  const lc=$('#lastCheck');
  if(lc) lc.textContent = STATE.last_ingest
      ? ('Last updated '+timeAgo(STATE.last_ingest)+' · via the browser extension')
      : 'No data yet · open a series page in your browser (with the extension installed) to collect it';
  const c=STATE.counts;
  $('#countLine').textContent=`${c.total} tracked · ${c.out} out · ${c.announced} announced · ${c.waiting} waiting`;
  renderHero();renderOverdue();renderGrid();renderActivity();
  checkForNotifications();
}

function renderOverdue(){
  const list=STATE.overdue||[];
  const wrap=$('#overdueWrap'),box=$('#overdue');
  if(!wrap||!box) return;
  if(!list.length){wrap.style.display='none';box.innerHTML='';return;}
  wrap.style.display='block';
  const cnt=$('#overdueCount'); if(cnt) cnt.textContent='('+list.length+')';
  box.innerHTML=list.map(o=>{
    // count the days here rather than trusting the payload, so the number stays
    // right on a dashboard left open across midnight
    const exp=new Date(o.expected+'T00:00:00');
    const t=new Date();t.setHours(0,0,0,0);
    const days=isNaN(exp)?(o.days_late||0):Math.max(0,Math.round((t-exp)/86400000));
    const cov=o.cover?`<img class="cov" src="${attrEsc(o.cover)}" alt="" onerror="this.remove()">`:'';
    const bk=o.book_n!=null?` · book ${o.book_n}`:'';
    const s=STATE.series.find(x=>x.id===o.series_id)||{};
    const title=wrapLink(esc(o.title),storeLink(s,o.book_n,'novel'),'Open on Amazon');
    return `<div class="od"><div class="late">${days}<small>${days===1?'day':'days'} late</small></div>${cov}
      <div class="info"><div class="t">${title}</div>
      <div class="d">expected ~ ${esc(fmtDate(o.expected))}${bk} · ${esc(o.series)}</div></div></div>`;
  }).join('');
}

// ---- release-day timer ----
// Target instant = release date at the hour this series usually goes live
// (learned from past confirmed releases: its own, then everyone's, then 2pm).
function releaseTarget(u){
  const h=Number(u.expected_hour==null?14:u.expected_hour);
  const d=new Date(u.date+'T00:00:00'); if(isNaN(d)) return null;
  d.setHours(Math.floor(h),Math.round((h%1)*60),0,0);
  return d.getTime();
}
function fmtDur(ms){
  ms=Math.abs(ms); const s=Math.floor(ms/1000);
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
  if(h>=48) return Math.floor(h/24)+'d '+(h%24)+'h';
  return (h>0?h+'h ':'')+String(m).padStart(2,'0')+'m '+String(sec).padStart(2,'0')+'s';
}
function releaseState(u){
  if(u.estimate) return {state:'plain'};
  if(u.live){
    let when='out now';
    if(u.live_at){const t=new Date(u.live_at); if(!isNaN(t)) when='live since '+t.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
    return {state:'released', when};
  }
  const target=releaseTarget(u); if(target==null) return {state:'plain'};
  const now=Date.now();
  if(u.countdown_days!==0 && now<target-36*3600*1000) return {state:'plain'};   // not release day yet
  const basisWord={series:'this series usually goes live',global:'your series usually go live',default:'typical US release time'}[u.expected_hour_basis]||'expected';
  const hh=new Date(target).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  if(now<target) return {state:'countdown', target, text:fmtDur(target-now), sub:'until ~'+hh, basis:basisWord};
  return {state:'late', target, text:'−'+fmtDur(now-target), sub:'past ~'+hh+' · not yet seen live', basis:basisWord};
}
// keep every visible timer moving without re-rendering the whole hero
setInterval(()=>{
  document.querySelectorAll('.count.tick').forEach(el=>{
    const target=+el.dataset.target; if(!target) return;
    const now=Date.now(); const late=now>=target;
    el.classList.toggle('countdown',!late); el.classList.toggle('late',late);
    el.closest('.up')?.classList.toggle('late',late);
    const small=el.querySelector('small'); const subTxt=small?small.textContent:'';
    el.firstChild.nodeValue=(late?'−':'')+fmtDur(target-now);
    if(late && small && !/past/.test(subTxt)) small.textContent=subTxt.replace(/^until/,'past')+' · not yet seen live';
  });
},1000);

function renderHero(){
  const up=STATE.upcoming||[];
  const wrap=$('#heroWrap');const hero=$('#hero');
  const hasSeries=(STATE.series||[]).length>0;
  if(!hasSeries){wrap.style.display='none';return;}
  wrap.style.display='block';
  const mode=STATE.upcoming_mode||'audiobook';
  const isNovel=mode==='novel';
  const ht=$('#heroTitle'); if(ht) ht.textContent=isNovel?'Next novel releases':'Next audiobook releases';
  const mt=$('#modeToggle'); if(mt) mt.textContent=isNovel?'⇄ Show audiobooks':'⇄ Show novels';
  const estOn=isNovel?!!STATE.show_estimates_novel:!!STATE.show_estimates;
  const tb=$('#estToggle');
  if(tb){
    tb.textContent=estOn?'Estimates: on':'Estimates: off';
    tb.style.color=estOn?'var(--amber)':'var(--muted)';
    tb.style.borderColor=estOn?'#5a4a25':'var(--line)';
    tb.title=isNovel
      ? "Include the next book in each series, estimated from how far apart past novels released"
      : "Include likely audiobooks not yet announced, estimated from each series' past novel-to-audio timing";
  }
  hero.innerHTML='';
  if(!up.length){
    const kindWord=isNovel?'novels':'audiobooks';
    hero.innerHTML='<div style="color:var(--faint);padding:14px 2px;font-size:13px">No upcoming '+kindWord+(estOn?'':' — turn on estimates to include predicted releases')+'.</div>';
    return;}
  up.forEach((u,i)=>{
    const el=document.createElement('div');el.className='up'+(i===0?' soonest':'');
    const note=(u.estimate&&u.note)?` <span class="hint" style="margin:0">· ${esc(u.note)}</span>`:'';
    const conf=u.estimate?confChip(u.confidence):'';
    const kindCls=u.kind==='novel'?'novel':'audio';
    const kindLbl=u.kind==='novel'?'Novel':'Audiobook';
    // Release day gets three states instead of a useless "0 days":
    //   released  -> green RELEASED (the store confirmed it buyable)
    //   before    -> live countdown to the hour this series usually goes live
    //   after     -> red negative timer: it's late against that expected hour
    const st=releaseState(u);
    if(st.state==='released') el.classList.add('released');
    else if(st.state==='late') el.classList.add('late');
    let countHtml;
    if(st.state==='released') countHtml=`<div class="count rel">RELEASED<small>${esc(st.when)}</small></div>`;
    else if(st.state==='countdown'||st.state==='late')
      countHtml=`<div class="count tick ${st.state}" data-target="${st.target}" data-basis="${esc(st.basis)}">${st.text}<small>${esc(st.sub)}</small></div>`;
    else countHtml=`<div class="count">${u.countdown_days}<small>${u.countdown_days===1?'day':'days'}</small></div>`;
    el.innerHTML=`<div>${countHtml}</div>
      <div><div class="t">${esc(u.title)}</div>
      <div class="d">${fmtDate(u.date)} · <span class="kind ${kindCls} ${u.estimate?'est':''}">${kindLbl}${u.estimate?' · est':''}</span>${conf}${note}</div></div>`;
    hero.appendChild(el);
  });
}

function timelineHtml(s){
  // one entry per unique date, tracking the book number(s) on that date plus
  // whatever the entry knows for the hover card (title, cover, narrator)
  const ev={};
  const add=(d,k,num,meta)=>{if(!d)return;const e=(ev[d]=ev[d]||{});e[k]=true;
    if(num!=null){if(k==='novel')e.nN=num;else if(k==='audio')e.aN=num;}
    if(meta){if(meta.title&&!e.title)e.title=meta.title;if(meta.cover&&!e.cover)e.cover=meta.cover;
      if(k==='audio'&&meta.narrator)e.narr=meta.narrator;}};
  const tl=(s.timeline&&s.timeline.length)?s.timeline:null;
  const live={};                                                 // date -> {n:true,a:true} = confirmed buyable
  const mklive=(d,k)=>{if(d)(live[d]=live[d]||{})[k]=true;};
  if(tl){tl.forEach(t=>{add(t.novel,'novel',t.n,t);add(t.audiobook,'audio',t.n,t);
    if(t.novel_pre===false)mklive(t.novel,'n');if(t.audio_pre===false)mklive(t.audiobook,'a');});}
  else{(s.history||[]).forEach(h=>{add(h.novel,'novel',h.n);add(h.audiobook,'audio',h.n);});}
  add(s.novel_date,'novel',s.novel_n);                          // always include the tracked book
  if(s.audiobook_status!=='none') add(s.audiobook_date,'audio',s.novel_n);
  // dashed ring only for a still-future estimate; a lapsed one is reported as
  // "overdue" in the predict line, not drawn as if it were ahead of us
  if((s.audiobook_status==='none'||s.audiobook_status==='unknown')&&s.predicted_date&&isReleased(s.predicted_date)===false) add(s.predicted_date,'est',null);

  const dates=Object.keys(ev).filter(d=>!isNaN(+new Date(d+'T00:00:00')));
  if(!dates.length) return '';
  const times=dates.map(d=>+new Date(d+'T00:00:00'));
  const min=Math.min(...times),max=Math.max(...times),span=(max-min)||1;
  const isLive=(d,e)=>{const lv=live[d]||{};return (!e.novel||lv.n)&&(!e.audio||lv.a);};
  const rel=(d,e)=>{const r=isReleased(d);return r===null?'':(r?' (out)':(isTodayStr(d)&&isLive(d,e)?' (out now)':' (pre-order)'));};
  const marks=dates.map(d=>{
    const t=+new Date(d+'T00:00:00');
    const left=dates.length===1?50:((t-min)/span*100);
    const e=ev[d];let cls,title;
    const nn=e.nN!=null?(' #'+e.nN):'', an=e.aN!=null?(' #'+e.aN):'';
    if(e.novel&&e.audio){cls='both';title=`Novel${nn} + Audiobook${an} · ${fmtDate(d)}${rel(d,e)}`;}
    else if(e.novel){cls='novel';title=`Novel${nn} · ${fmtDate(d)}${rel(d,e)}`;}
    else if(e.audio){cls='audio';title=`Audiobook${an} · ${fmtDate(d)}${rel(d,e)}`;}
    else{cls='est';title=`Estimated audiobook · likely ~${fmtDate(d)}`;}
    if(cls!=='est'&&isReleased(d)===false&&!(isTodayStr(d)&&isLive(d,e))) cls+=' future';   // hollow = still upcoming; confirmed-live today renders solid
    const mUrl=e.novel?storeLink(s,e.nN,'novel'):(e.audio?storeLink(s,e.aN,'audio'):null);
    const isEst=cls==='est';
    const _bn=(e.aN!=null?e.aN:e.nN);
    const _hasStore=_bn!=null&&(((s.asins_audio||{})[_bn])||((s.asins_novel||{})[_bn])||((s.asins||{})[_bn]));
    const tip={sid:s.id,buyN:(!isEst&&_hasStore)?_bn:null,
      sn:s.name||'',label:title,t:e.title||null,cover:e.cover||s.cover_url||null,
      cover2:(e.cover&&s.cover_url&&e.cover!==s.cover_url)?s.cover_url:null,
      narr:e.narr||null,nN:e.nN??null,aN:e.aN??null,est:isEst,
      estN:isEst?(s.predicted_book_n??null):null,
      estWhy:isEst?(s.predicted_queued
        ? (s.predicted_after_n!=null?`queued behind book ${s.predicted_after_n}'s audiobook, at this series' usual pace`:'next in this series’ audio queue')
        : 'projected from this series’ past novel→audio gaps'):null};
    const attrs=`class="mark ${cls}" style="left:${left.toFixed(1)}%" aria-label="${esc(title)}" data-tip="${attrEsc(JSON.stringify(tip))}"`;
    return mUrl?`<a ${attrs} href="${esc(mUrl)}" target="_blank" rel="noopener"></a>`:`<span ${attrs}></span>`;
  }).join('');
  // "today" line: only when it falls within the span, so past sits left / upcoming right
  const now=Date.now();let today='';
  if(dates.length>1&&now>=min&&now<=max){const tl=((now-min)/span*100);today=`<span class="tl-today" style="left:${tl.toFixed(1)}%" title="today"></span>`;}
  const lbl=s.avg_gap_label?`avg gap ${s.avg_gap_label}`:(dates.length>1?'releases':'release');
  // pin the latest (rightmost) date under its mark, always visible
  let endLbl='';
  {
    const lastD=dates.find(d=>(+new Date(d+'T00:00:00'))===max);
    if(lastD){
      const e=ev[lastD]||{};
      const isEst=!e.novel&&!e.audio;
      const left=dates.length===1?50:100;
      endLbl=`<span class="tl-end" style="left:${left}%;transform:translateX(-50%)">${isEst?'~':''}${esc(fmtDate(lastD))}</span>`;
    }
  }
  return `<div class="timeline"><div class="tl-row">
      <span class="gaplabel">${lbl}</span>
      <span class="track">${today}${marks}${endLbl}</span>
    </div></div>`;
}

function timeAgo(iso){
  if(!iso) return 'not yet';
  const t=new Date(iso); if(isNaN(t)) return '';
  const s=Math.floor((Date.now()-t)/1000);
  if(s<60) return 'just now';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function initials(name){return (name||'?').replace(/^(the|a|an)\s+/i,'').trim().charAt(0).toUpperCase()||'?';}

function sortedSeries(){
  const arr=STATE.series.slice();
  const mode=$('#sortSel')?$('#sortSel').value:'manual';
  if(mode==='title') arr.sort((a,b)=>(a.name||'').localeCompare(b.name||''));
  else if(mode==='next') arr.sort((a,b)=>{
    const x=a.next_date||'9999',y=b.next_date||'9999';return x<y?-1:x>y?1:0;});
  else if(mode==='status'){const rank={announced:0,none:1,unknown:1,released:2};
    arr.sort((a,b)=>(rank[a.status_class||a.audiobook_status]??1)-(rank[b.status_class||b.audiobook_status]??1));}
  return arr; // 'manual' keeps stored order
}

function renderGrid(){
  const grid=$('#grid');const empty=$('#empty');
  const has=STATE.series.length>0;
  $('#seriesEyebrow').style.display=has?'block':'none';
  $('#sortbar').style.display=has?'flex':'none';
  const lg=$('#legend'); if(lg) lg.style.display=has?'flex':'none';
  if(!has){empty.style.display='block';grid.innerHTML='';return;}
  empty.style.display='none';grid.innerHTML='';
  const manual=!$('#sortSel')||$('#sortSel').value==='manual';
  sortedSeries().forEach(s=>{
    const card=document.createElement('div');card.className='card';card.dataset.id=s.id;
    if(manual) card.setAttribute('draggable','true');
    const st=s.audiobook_status;
    const mode=STATE.upcoming_mode||'audiobook';
    let predict='';
    if(mode==='novel'){
      if(STATE.show_estimates_novel && s.predicted_novel_date){
        predict=`<div class="predict">Likely next novel <span class="mono">~ ${fmtDate(s.predicted_novel_date)}</span> · ${s.novel_history_count} past gap${s.novel_history_count===1?'':'s'} (${s.avg_novel_gap_label})${confChip(s.predicted_novel_confidence)}</div>`;
      }
    }else{
      if(st==='none'||st==='unknown'){
        // Confirmed dates and overdue counts both live in the Audiobook column
        // now, so this line carries only a genuine forward-looking forecast. A
        // lapsed projection says nothing here — it's the column's red count and
        // the Overdue panel that report it.
        if(s.predicted_date&&isReleased(s.predicted_date)===false){
          const bk=s.predicted_book_n!=null?` book ${s.predicted_book_n}`:'';
          // say what the guess is built on: the audio queue when a confirmed date
          // ahead of it sets the pace, the novel→audio gap history otherwise
          const basis=s.predicted_queued
            ? (s.predicted_after_n!=null?`queued after book ${s.predicted_after_n}'s audio`:'next in the audio queue')
            : `${s.history_count} past gap${s.history_count===1?'':'s'} (${s.avg_gap_label})`;
          predict=`<div class="predict">Likely audiobook${bk} <span class="mono">~ ${fmtDate(s.predicted_date)}</span> · ${basis}${confChip(s.predicted_confidence)}</div>`;
        }
      }
    }
    const err=s.fetch_error?`<div class="err">⚠ ${esc(s.fetch_error)}</div>`:'';
    const chgTime=s.last_change_at?` <span class="chg-time" title="${esc(new Date(s.last_change_at).toLocaleString())}">· ${timeAgo(s.last_change_at)}</span>`:'';
    const chg=s.last_change?`<div class="changed">● ${esc(s.last_change)}${chgTime}</div>`:'';
    const cover=s.cover_url
      ? `<img class="cover" src="${attrEsc(s.cover_url)}" alt="" onerror="this.outerHTML='<div class=&quot;cover ph&quot;>${esc(initials(s.name))}</div>'">`
      : `<div class="cover ph">${esc(initials(s.name))}</div>`;
    const bookUrl=storeLink(s,(latestEntry(s)||{}).n!=null?(latestEntry(s)||{}).n:s.novel_n,'novel');
    card.innerHTML=`
      ${manual?'<div class="grip" title="Drag to reorder">⋮⋮</div>':''}
      ${wrapLink(cover,bookUrl,'Open on Amazon')}
      <div class="cmain">
        ${wrapLink(`<h3>${esc(s.name)}</h3>`,bookUrl,'Open the latest book on Amazon')}
        <div class="byline">${s.author?wrapLink(esc(s.author),authorLink(s),'Search this author on Amazon'):'<span style="color:var(--faint)">unknown author</span>'}${s.novel_title?' · '+wrapLink(esc(s.novel_title),bookUrl):''}</div>
        <div class="facts">
          <div class="fact"><div class="k">Novel</div><div class="v">${bkTag(novelBookN(s))}${fmtDate(s.novel_date)}${dateTag(s.novel_date,(latestEntry(s)||{}).novel_pre===false)}</div></div>
          <div class="fact"><div class="k">Audiobook</div><div class="v">${audioFact(s)}</div></div>
          <div class="fact"><div class="k">Avg gap</div><div class="v">${s.avg_gap_label||'—'}</div></div>
          ${s.narrator?`<div class="fact"><div class="k">Narrator</div><div class="v">${esc(s.narrator)}</div></div>`:''}
          ${s.listening?`<div class="fact"><div class="k">Listening</div><div class="v listening" title="From Audiobookshelf">🎧 book ${s.listening.n??'?'} · ${Math.round(s.listening.pct)}%</div></div>`:''}
        </div>
        ${s.intel?`<div class="intel" title="AI web research · ${attrEsc((s.intel.at||'').slice(0,16).replace('T',' '))} · confidence: ${attrEsc(s.intel.confidence||'?')}">🔎 ${esc(s.intel.summary||s.intel.status||'')}${(s.intel.next_book&&s.intel.next_book.expected)?` · next: book ${esc(String(s.intel.next_book.n??'?'))} ~${esc(s.intel.next_book.expected)}`:''}</div>`:''}
        <span class="pill ${s.status_class||st}">${esc(s.status_label)}</span>
        ${predict}${chg}${err}
        ${timelineHtml(s)}
      </div>
      <div class="right">
        <div class="rowbtns">
          <button class="small" data-check="${s.id}" title="Ask the extension to re-check just this series for new or newly-released books. Needs the extension running with Firefox open; takes a few seconds.">Check</button>
          <button class="small" data-edit="${s.id}">Edit</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
  wireDrag();
  grid.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openModal(b.dataset.edit));
  grid.querySelectorAll('[data-check]').forEach(b=>b.onclick=ev=>showCheckMenu(ev.currentTarget,b.dataset.check));
}

/* ---- Check menu: quick Amazon re-read vs the AI web-research check ---- */
let CHECKMENU=null;
function hideCheckMenu(){if(CHECKMENU){CHECKMENU.remove();CHECKMENU=null;}}
function showCheckMenu(anchor,sid){
  hideCheckMenu();
  const s=STATE.series.find(x=>x.id===sid)||{};
  const nm=s.name||'this series';
  CHECKMENU=document.createElement('div');
  CHECKMENU.className='checkmenu';
  CHECKMENU.innerHTML=`
    <button class="small" data-cm="amazon">\ud83d\udcc4 Quick check \u2014 re-read the Amazon pages</button>
    <button class="small" data-cm="ai">\ud83d\udd0e AI check \u2014 web research on what's known${s.intel?` <span class="cm-sub">last: ${esc((s.intel.at||'').slice(0,10))}</span>`:''}</button>
    <button class="small ghost" data-cm="x">Cancel</button>`;
  document.body.appendChild(CHECKMENU);
  const r=anchor.getBoundingClientRect();
  CHECKMENU.style.left=Math.min(r.left,window.innerWidth-CHECKMENU.offsetWidth-10)+'px';
  CHECKMENU.style.top=Math.min(r.bottom+6,window.innerHeight-CHECKMENU.offsetHeight-10)+'px';
  CHECKMENU.addEventListener('click',async ev=>{
    const b=ev.target.closest('button[data-cm]');if(!b)return;
    const act=b.dataset.cm;hideCheckMenu();
    if(act==='amazon'){
      askExtension('/api/ext/crawl-series','Checking \u201c'+nm+'\u201d',{id:sid});
    }else if(act==='ai'){
      try{const rr=await api('/api/series/'+sid+'/research',{method:'POST'});
          toast(rr.message||(rr.ok?'AI check started':'Failed'));}
      catch(e){toast('Failed: '+e);}
    }
  });
  setTimeout(()=>document.addEventListener('click',function outside(e){
    if(CHECKMENU&&!CHECKMENU.contains(e.target))hideCheckMenu();
    document.removeEventListener('click',outside);
  }),0);
}

/* ---- drag to reorder ---- */
let DRAG_ID=null;
function wireDrag(){
  const grid=$('#grid');
  grid.querySelectorAll('.card[draggable="true"]').forEach(card=>{
    card.ondragstart=e=>{
      // don't start a drag from a button
      if(e.target.closest('button,a,select,input')){e.preventDefault();return;}
      DRAG_ID=card.dataset.id;card.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
      try{e.dataTransfer.setData('text/plain',DRAG_ID);}catch(_){}
    };
    card.ondragend=()=>{card.classList.remove('dragging');grid.querySelectorAll('.card').forEach(c=>c.classList.remove('dragover'));};
    card.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='move';
      grid.querySelectorAll('.card').forEach(c=>c.classList.remove('dragover'));card.classList.add('dragover');};
    card.ondrop=e=>{
      e.preventDefault();card.classList.remove('dragover');
      if(!DRAG_ID||DRAG_ID===card.dataset.id)return;
      const src=grid.querySelector('.card[data-id="'+DRAG_ID+'"]');
      if(!src)return;
      const rect=card.getBoundingClientRect();
      const after=e.clientY>rect.top+rect.height/2;
      grid.insertBefore(src, after?card.nextSibling:card);
      persistOrder();
    };
  });
}
async function persistOrder(){
  const ids=[...$('#grid').querySelectorAll('.card')].map(c=>c.dataset.id);
  // reorder STATE to match so re-renders keep the new order
  STATE.series.sort((a,b)=>ids.indexOf(a.id)-ids.indexOf(b.id));
  try{await api('/api/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});}catch(_){}
}

function renderActivity(){
  const a=$('#activity');const rows=(STATE.log||[]).slice(0,6);
  a.innerHTML=rows.map(r=>`<div>${new Date(r.time).toLocaleString()} — ${esc(r.msg)}</div>`).join('');
}

/* ---- modal ---- */
function histRow(num='',nov='',aud=''){
  const div=document.createElement('div');div.className='hist-row';
  div.innerHTML=`<input class="bnum" placeholder="#" value="${esc(num)}" title="Book number" style="max-width:46px;text-align:center">
    <input placeholder="novel date" value="${esc(nov)}"><input placeholder="audiobook date" value="${esc(aud)}">
    <button class="small ghost" type="button">✕</button>`;
  div.querySelector('button').onclick=()=>div.remove();
  return div;
}
function openModal(id){
  $('#histRows').innerHTML='';
  if(id){
    const s=STATE.series.find(x=>x.id===id);
    $('#mTitle').textContent='Edit series';
    $('#f_id').value=s.id;$('#f_url').value=s.url||'';$('#f_name').value=s.name||'';
    $('#f_author').value=s.author||'';$('#f_novel_title').value=s.novel_title||'';
    $('#f_novel_date').value=s.novel_date||'';$('#f_status').value=s.audiobook_status||'unknown';
    $('#f_audio_date').value=s.audiobook_date||'';$('#f_notes').value=s.notes||'';
    $('#f_buy_mode').value=s.buy_mode||'off';$('#f_buy_cap').value=(s.buy_cap==null?'':s.buy_cap);
    const rows=(s.timeline&&s.timeline.length)?s.timeline:(s.history||[]);
    rows.forEach(h=>$('#histRows').appendChild(histRow(h.n??'',h.novel||'',h.audiobook||'')));
    $('#deleteBtn').style.visibility='visible';
  }else{
    $('#mTitle').textContent='Add series';
    ['f_id','f_url','f_name','f_author','f_novel_title','f_novel_date','f_audio_date','f_notes','f_buy_cap'].forEach(k=>$('#'+k).value='');
    $('#f_status').value='unknown';$('#f_buy_mode').value='off';
    $('#deleteBtn').style.visibility='hidden';
  }
  $('#scrim').classList.add('open');
}
function closeModal(){$('#scrim').classList.remove('open');}
$('#addHistBtn').onclick=()=>$('#histRows').appendChild(histRow());

$('#autofillBtn').onclick=async()=>{
  const url=$('#f_url').value.trim();if(!url){toast('Paste a link first');return;}
  $('#autofillBtn').textContent='…';
  const r=await api('/api/fetch-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
  $('#autofillBtn').textContent='Auto-fill';
  if(r.error){toast(r.error);return;}
  if(!$('#f_name').value)$('#f_name').value=r.series_guess||r.title||'';
  if(r.title&&!$('#f_novel_title').value)$('#f_novel_title').value=r.title;
  if(r.author&&!$('#f_author').value)$('#f_author').value=r.author;
  if(r.date&&!$('#f_novel_date').value)$('#f_novel_date').value=r.date;
  if(r.audiobook_date&&!$('#f_audio_date').value){
    $('#f_audio_date').value=r.audiobook_date;
    const ad=new Date(r.audiobook_date+'T00:00:00');
    $('#f_status').value=(!isNaN(ad)&&ad<=new Date())?'released':'announced';
  } else if(r.is_audiobook&&r.date&&!$('#f_audio_date').value){
    $('#f_audio_date').value=r.date;$('#f_status').value='released';
  }
  const src=(r.sources||[]).filter(s=>s!=='page');
  toast(src.length?('Filled from '+src.join(' + ')):'Filled what I could find');
};

$('#saveBtn').onclick=async()=>{
  const timeline=[...document.querySelectorAll('.hist-row')].map(r=>{
    const i=r.querySelectorAll('input');
    const num=i[0].value.trim();
    return {n:num===''?null:parseInt(num,10),novel:i[1].value.trim(),audiobook:i[2].value.trim()};
  }).filter(h=>h.novel||h.audiobook||h.n!=null);
  const body={
    name:$('#f_name').value.trim(),author:$('#f_author').value.trim(),url:$('#f_url').value.trim(),
    novel_title:$('#f_novel_title').value.trim(),novel_date:$('#f_novel_date').value.trim(),
    audiobook_date:$('#f_audio_date').value.trim(),audiobook_status:$('#f_status').value,
    notes:$('#f_notes').value.trim(),timeline,
    buy_mode:$('#f_buy_mode').value,buy_cap:$('#f_buy_cap').value.trim()
  };
  if(!body.name){toast('A name is required');return;}
  const id=$('#f_id').value;
  const res=await api(id?('/api/series/'+id):'/api/series',
    {method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(res.error){toast(res.error);return;}
  closeModal();toast(id?'Saved':'Added');refresh();
};
const _rsb=$('#researchBtn');
if(_rsb) _rsb.onclick=async()=>{
  const id=$('#f_id').value; if(!id){toast('Save the series first');return;}
  _rsb.disabled=true;_rsb.textContent='Researching…';
  try{const r=await api('/api/series/'+id+'/research',{method:'POST'});
      $('#researchMsg').textContent=r.ok?('Done: '+r.message+' — see the card'):(r.message||'Failed');}
  catch(e){$('#researchMsg').textContent='Failed: '+e;}
  _rsb.disabled=false;_rsb.textContent='🔎 Research this series';refresh();
};
$('#deleteBtn').onclick=async()=>{
  const id=$('#f_id').value;if(!id)return;
  if(!confirm('Remove this series from the radar?'))return;
  await api('/api/series/'+id,{method:'DELETE'});closeModal();toast('Removed');refresh();
};
$('#scrim').onclick=e=>{if(e.target===$('#scrim'))closeModal();};

/* ---- settings ---- */
let REMOTE_RO=false;
async function loadDiscordSettings(){
  try{
    const s=await api('/api/settings');
    if(s.remote_readonly && !REMOTE_RO){
      REMOTE_RO=true;
      document.body.insertAdjacentHTML('afterbegin',
        '<div class="remote-ro">Viewing over the tunnel — read-only. Edits, crawls and settings only work on the PC.</div>');
    }
    $('#discWebhook').value=s.discord_webhook||(s.discord_webhook_set?'(set — hidden on this device)':'');
    $('#discWebhook').disabled=!!s.remote_readonly;
    $('#discOnNew').checked=!!s.discord_on_new;
    $('#discOnRelease').checked=!!s.discord_on_release;
    $('#discDaily').checked=!!s.discord_daily;
    $('#discDailyHour').value=(s.discord_daily_hour!=null?s.discord_daily_hour:9);
    $('#discDigestDays').value=String(s.discord_digest_days||1);
  }catch(_){}
}
async function saveDiscord(){
  if(REMOTE_RO) return;                      // never post the masked value back
  const body={
    discord_webhook:$('#discWebhook').value.trim(),
    discord_on_new:$('#discOnNew').checked,
    discord_on_release:$('#discOnRelease').checked,
    discord_daily:$('#discDaily').checked,
    discord_daily_hour:parseInt($('#discDailyHour').value,10),
    discord_digest_days:parseInt($('#discDigestDays').value,10)
  };
  try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(_){}
}
['#discWebhook','#discOnNew','#discOnRelease','#discDaily','#discDailyHour','#discDigestDays'].forEach(sel=>{
  const el=$(sel); if(el) el.addEventListener('change',saveDiscord);
});
const _discTest=$('#discTestBtn');
if(_discTest) _discTest.onclick=async()=>{
  const msg=$('#discTestMsg'); msg.textContent='Sending…'; msg.style.color='var(--faint)';
  await saveDiscord();
  try{
    const d=await api('/api/discord/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({discord_webhook:$('#discWebhook').value.trim()})});
    msg.textContent=d.message||(d.ok?'Sent!':'Failed'); msg.style.color=d.ok?'var(--green)':'var(--red)';
  }catch(e){ msg.textContent='Failed: '+e; msg.style.color='var(--red)'; }
};
const _digNow=$('#digNowBtn');
if(_digNow) _digNow.onclick=async()=>{
  const msg=$('#digNowMsg'); msg.textContent='Sending\u2026'; msg.style.color='var(--faint)';
  await saveDiscord();
  try{ const d=await api('/api/discord/digest-now',{method:'POST'});
       msg.textContent=d.message||(d.ok?'Sent!':'Failed'); msg.style.color=d.ok?'var(--green)':'var(--red)'; }
  catch(e){ msg.textContent='Failed: '+e; msg.style.color='var(--red)'; }
};
/* ---- auto-buy settings + jobs ---- */
async function loadBuySettings(){
  try{
    const s=await api('/api/settings');
    $('#buyMaster').checked=!!s.buy_master; $('#buyDry').checked=!!s.buy_dry_run;
    $('#buyCapDefault').value=(s.buy_cap_default!=null?s.buy_cap_default:10);
    $('#buyWeeklyMax').value=(s.buy_weekly_max!=null?s.buy_weekly_max:3);
    $('#buyEmailTo').value=s.buy_email_to||''; $('#buySmtpUser').value=s.buy_smtp_user||'';
    $('#buySmtpPass').value=''; $('#buySmtpPassHint').textContent=s.buy_smtp_pass_set?'A password is saved. Type a new one to replace it.':'Not set yet.';
    $('#buyPublicBase').value=s.buy_public_base||''; $('#buyBat').value=s.buy_bat||'';
    $('#buyBatHint').textContent=s.buy_bat_found?('Using: '+s.buy_bat_found):'Not found — paste the full path to Update Audiobooks.bat.';
    ['#buyMaster','#buyDry','#buyCapDefault','#buyWeeklyMax','#buyEmailTo','#buySmtpUser','#buySmtpPass','#buyPublicBase','#buyBat'].forEach(sel=>{const el=$(sel); if(el) el.disabled=!!s.remote_readonly;});
  }catch(_){}
  loadBuyJobs();
}
async function saveBuy(){
  if(REMOTE_RO) return;
  const body={buy_master:$('#buyMaster').checked,buy_dry_run:$('#buyDry').checked,
    buy_cap_default:$('#buyCapDefault').value,buy_weekly_max:parseInt($('#buyWeeklyMax').value,10),
    buy_email_to:$('#buyEmailTo').value.trim(),buy_smtp_user:$('#buySmtpUser').value.trim(),
    buy_public_base:$('#buyPublicBase').value.trim(),buy_bat:$('#buyBat').value.trim()};
  const pw=$('#buySmtpPass').value.trim(); if(pw) body.buy_smtp_pass=pw;
  try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(pw){$('#buySmtpPass').value='';$('#buySmtpPassHint').textContent='Saved.';}}catch(_){}
}
['#buyMaster','#buyDry','#buyCapDefault','#buyWeeklyMax','#buyEmailTo','#buySmtpUser','#buySmtpPass','#buyPublicBase','#buyBat'].forEach(sel=>{
  const el=$(sel); if(el) el.addEventListener('change',saveBuy);
});
const STATUS_CLASS={done:'good',dry_done:'good',owned:'good',running:'wait',probing:'wait',waiting_offer:'wait',awaiting_approval:'wait',approved:'wait',
  failed:'bad',uncertain:'bad',needs_signin:'bad',denied:'bad',expired:'bad',cancelled:'bad',done_unreturned:'wait'};
const STATUS_TEXT={probing:'reading the page…',waiting_offer:'waiting for the audiobook offer',awaiting_approval:'WAITING FOR YOUR OK',
  approved:'approved',running:'extension working…',dry_done:'dry run done — nothing bought',done:'bought ✓',done_unreturned:'bought — return the KU borrow by hand',
  uncertain:'UNCERTAIN — check Amazon; blocked from retrying',owned:'already owned',failed:'failed',denied:'denied',expired:'link expired',
  cancelled:'cancelled',needs_signin:'Firefox not signed in to Amazon'};
async function loadBuyJobs(){
  const box=$('#buyJobs'); if(!box) return;
  try{
    const d=await api('/api/buy/jobs');
    if(!d.jobs||!d.jobs.length){box.textContent='No jobs yet. They appear when a tracked audiobook with auto-buy on goes live.';return;}
    box.innerHTML=d.jobs.map(j=>{
      const cls=STATUS_CLASS[j.status]||'';
      const price=(j.price!=null)?('$'+Number(j.price).toFixed(2)):'price ?';
      const steps=(j.steps||[]).slice(-6).map(st=>`${st.at.slice(5,16).replace('T',' ')} ${st.ok?'ok ':'!! '}${st.step}: ${st.detail||''}`).join('\n');
      const acts=[];
      if(j.status==='awaiting_approval') acts.push(`<button class="small" data-act="approve" data-id="${j.id}">Approve</button><button class="small ghost" data-act="deny" data-id="${j.id}">Deny</button>`);
      if(['failed','expired','denied','dry_done','waiting_offer','needs_signin','cancelled'].includes(j.status)) acts.push(`<button class="small ghost" data-act="retry" data-id="${j.id}">Retry</button>`);
      if(['probing','waiting_offer','awaiting_approval'].includes(j.status)) acts.push(`<button class="small ghost" data-act="cancel" data-id="${j.id}">Cancel</button>`);
      if(!['running','approved','probing'].includes(j.status)) acts.push(`<button class="small ghost" data-act="forget" data-id="${j.id}">Remove</button>`);
      return `<div class="bj"><b>${esc(j.series||'')}</b> — book ${j.n??''} · ${price} · ${j.mode}${j.dry_run?' · DRY':''}<span class="st ${cls}">${esc(STATUS_TEXT[j.status]||j.status)}</span>
        ${j.note?`<div class="hint" style="margin:2px 0 0">${esc(j.note)}</div>`:''}
        ${steps?`<div class="steps">${esc(steps)}</div>`:''}
        ${acts.length?`<div class="acts">${acts.join('')}</div>`:''}</div>`;
    }).join('')+`<div class="hint">${d.week_auto} automatic buy(s) in the last 7 days.</div>`;
    box.querySelectorAll('button[data-act]').forEach(b=>b.onclick=async()=>{
      const r=await api('/api/buy/'+b.dataset.id+'/'+b.dataset.act,{method:'POST'});
      if(r.error) toast(r.error); loadBuyJobs();
    });
  }catch(_){ box.textContent='Could not load jobs.'; }
}
async function loadHolds(){
  const box=$('#heldBooks'); if(!box) return;
  try{
    const d=await api('/api/holds');
    if(!d.holds.length){box.textContent='None.';return;}
    box.innerHTML=d.holds.map((h,i)=>{
      const cands=(h.candidates&&h.candidates.length?h.candidates:d.authors);
      const opts=cands.map(c=>`<option value="${attrEsc(c)}">${esc(c)}</option>`).join('');
      return `<div class="bj"><b>${esc(h.book||'')}</b> — heard as ${esc(h.author||'?')}${h.series?(' / '+esc(h.series)):''}
        <div class="hint" style="margin:2px 0 4px">${esc(h.reason||'')}</div>
        <div class="acts"><select data-hold="${i}" style="width:auto;margin:0;padding:4px 6px">${opts}</select>
        <button class="small" data-resolve="${i}">Move there</button></div></div>`;
    }).join('');
    box.querySelectorAll('button[data-resolve]').forEach(b=>b.onclick=async()=>{
      const i=+b.dataset.resolve, h=d.holds[i];
      const sel=box.querySelector(`select[data-hold="${i}"]`);
      b.disabled=true;b.textContent='Moving…';
      const r=await api('/api/holds/resolve',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({src:h.src,author:sel.value,series:h.series||''})});
      toast(r.ok?('Moved'+(r.learned&&r.learned.length?' — learned '+r.learned.join('; '):'')):(r.error||'Failed'));
      loadHolds();
    });
  }catch(_){box.textContent='Could not load.';}
}
const _bte=$('#buyTestEmailBtn');
if(_bte) _bte.onclick=async()=>{ const m=$('#buyMsg'); m.textContent='Sending…'; await saveBuy();
  try{const d=await api('/api/buy/test-email',{method:'POST'}); m.textContent=d.message||(d.ok?'Sent':'Failed'); m.style.color=d.ok?'var(--green)':'var(--red)';}
  catch(e){m.textContent='Failed: '+e;} };
const _brb=$('#buyRunBatBtn');
if(_brb) _brb.onclick=async()=>{ const m=$('#buyMsg'); m.textContent='Starting…'; await saveBuy();
  try{const d=await api('/api/buy/run-bat',{method:'POST'}); m.textContent=d.message||(d.ok?'Running':'Failed'); m.style.color=d.ok?'var(--green)':'var(--red)';}
  catch(e){m.textContent='Failed: '+e;} };
const _bpb=$('#buyProbeBtn');
if(_bpb) _bpb.onclick=async()=>{ const m=$('#buyMsg'); const asin=$('#buyProbeAsin').value.trim().toUpperCase();
  try{const d=await api('/api/buy/test-probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio_asin:asin})});
      m.textContent=d.ok?'Checking… the result appears under Purchase jobs within about 30 seconds (the extension must be installed and Firefox open).':(d.error||'Failed'); m.style.color=d.ok?'var(--green)':'var(--red)'; setTimeout(loadBuyJobs,25000);}
  catch(e){m.textContent='Failed: '+e;} };
async function loadIntegrations(){
  try{
    const s=await api('/api/settings');
    $('#intAiProvider').value=s.ai_provider||'openrouter';
    $('#intAiModel').value=s.ai_model||'';
    $('#intAbsUrl').value=s.abs_url||'';
    $('#intBotToken').value=''; $('#intAiKey').value=''; $('#intAbsToken').value='';
    if(s.bot_token_set){
      const st=s.bot_running?'Bot online.':'Bot offline'+(s.bot_err?' — '+s.bot_err:' (starting, or restart the app after a token change).');
      $('#intBotHint').innerHTML=st+(s.bot_invite?' <a href="'+s.bot_invite+'" target="_blank" rel="noopener">Invite it to your server</a> (one-time; needs Manage Server).':'');
    } else $('#intBotHint').textContent='Discord → Developer Portal → New Application → Bot → Reset Token.';
    $('#intAiHint').textContent=($('#intAiProvider').value==='claude-code')?'No key needed — uses the Claude Code CLI installed on this PC (your subscription; its usage limits apply).':(s.ai_api_key_set?'A key is saved. Type a new one to replace it.':'Not set yet.');
    $('#intAbsHint').textContent=s.abs_token_set?'A token is saved. Type a new one to replace it.':'ABS → Settings → Users → your user → API token.';
    ['#intBotToken','#intAiProvider','#intAiKey','#intAiModel','#intAbsUrl','#intAbsToken'].forEach(sel=>{const el=$(sel); if(el) el.disabled=!!s.remote_readonly;});
  }catch(_){}
}
async function saveIntegrations(){
  if(REMOTE_RO) return;
  const body={ai_provider:$('#intAiProvider').value,ai_model:$('#intAiModel').value.trim(),abs_url:$('#intAbsUrl').value.trim()};
  const bt=$('#intBotToken').value.trim(); if(bt) body.bot_token=bt;
  const ak=$('#intAiKey').value.trim(); if(ak) body.ai_api_key=ak;
  const at=$('#intAbsToken').value.trim(); if(at) body.abs_token=at;
  try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(bt||ak||at){$('#intBotToken').value='';$('#intAiKey').value='';$('#intAbsToken').value='';loadIntegrations();}}catch(_){}
}
['#intBotToken','#intAiProvider','#intAiKey','#intAiModel','#intAbsUrl','#intAbsToken'].forEach(sel=>{
  const el=$(sel); if(el) el.addEventListener('change',saveIntegrations);
});
function openSettings(){ loadDiscordSettings(); loadBuySettings(); loadIntegrations(); loadHolds(); $('#settingsScrim').classList.add('open'); }
function closeSettings(){$('#settingsScrim').classList.remove('open');}
$('#settingsBtn').onclick=openSettings;
$('#estToggle').onclick=async()=>{
  const mode=STATE.upcoming_mode||'audiobook';
  const body=(mode==='novel')
    ? {show_estimates_novel:!STATE.show_estimates_novel}
    : {show_estimates:!STATE.show_estimates};
  await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(typeof refresh==='function') refresh();
};
$('#modeToggle').onclick=async()=>{
  const next=((STATE.upcoming_mode||'audiobook')==='audiobook')?'novel':'audiobook';
  await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upcoming_mode:next})});
  if(typeof refresh==='function') refresh();
};
$('#clearAllBtn').onclick=async()=>{
  if(!confirm("Delete ALL tracked series from the app AND tell the extension to forget every page?\n\nUse this for a true reset. (To just rebuild the app's data from pages the extension already knows, use 'Crawl all now' in the extension instead.)\n\nYour data.json is backed up first; settings are kept.")) return;
  const r=await api('/api/series/clear',{method:'POST',body:JSON.stringify({forget:true})});
  closeSettings();
  toast((r&&r.ok)?('Cleared '+r.removed+' series \u2014 extension will forget its pages shortly'):'Could not clear');
  if(typeof refresh==='function') refresh(); else location.reload();
};

/* ---- quit ---- */
$('#quitBtn').onclick=async()=>{
  if(!confirm('Close Release Radar? It stops collecting until you reopen it.'))return;
  try{await api('/api/quit',{method:'POST'});}catch(e){}
  document.body.innerHTML='<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;'+
    'font-family:var(--serif);font-size:22px;color:var(--muted);text-align:center;padding:40px">'+
    'Release Radar has stopped.<br><span style="font-size:15px">You can close this tab.</span></div>';
};

async function askExtension(path,label,body){
  try{ const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{method:'POST'};
       const r=await api(path,opt); if(!r||!r.ok){toast('Could not reach the app');return;} }
  catch(e){ toast('Could not reach the app'); return; }
  toast(label+' \u2014 the extension will start within ~20s. Make sure it\u2019s installed and Firefox is open.');
  // poll a little faster for a couple of minutes so results show as they land
  if(window.__extPoll) clearInterval(window.__extPoll);
  let n=0; window.__extPoll=setInterval(()=>{ refresh(); if(++n>40){clearInterval(window.__extPoll);window.__extPoll=null;} }, 3000);
}
$('#extRefreshBtn').onclick=()=>askExtension('/api/ext/refresh','Asked the extension to refresh dates');
$('#extCrawlBtn').onclick=()=>askExtension('/api/ext/crawl','Asked the extension to crawl all series');

$('#settingsScrim').onclick=e=>{if(e.target===$('#settingsScrim'))$('#settingsScrim').classList.remove('open');};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeSettings();}});

if($('#sortSel')) $('#sortSel').onchange=renderGrid;

refresh();
setInterval(refresh,60000);
