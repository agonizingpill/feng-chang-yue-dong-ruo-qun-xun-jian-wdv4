import {api,account} from '../api.js';
import {book} from '../notebook.js';
import {hiveName} from './notes.js';
import {text,num,toast,when,delay} from '../utils.js';
import {BATCH_STATES,PAIR_STATES} from '../constants.js';

const state={batch:null,detail:null,busy:false};

const closedDay=care=>{
  if(!care.closed_at)return null;
  return new Date(care.closed_at+'Z').toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});
};

function defaultRange(){
  const days=book.care.filter(c=>c.kind==='feed'&&c.status==='closed'&&c.closed_at).map(closedDay).filter(Boolean).sort();
  if(!days.length)return {from:'2026-11-01',to:'2026-11-30'};
  return {from:days[0],to:days[days.length-1]};
}

const statusBadge=value=>`<span class="badge ${value==='published'||value==='paired'?'closed':value==='failed'?'flagged':''}">${text(BATCH_STATES[value]||PAIR_STATES[value]||value)}</span>`;

function summaryLine(detail){
  const s=detail.summary||{};
  return `<div class="numbers"><div><strong>${s.selected??detail.cases.length}</strong><span>选入饲喂单</span></div><div><strong>${s.paired??detail.pairs.length}</strong><span>配成对照</span></div><div><strong>${s.unmatched??detail.unmatched.length}</strong><span>未配上</span></div><div><strong>${s.missing??detail.missing.length}</strong><span>缺测</span></div></div>`;
}

function pairTable(detail){
  if(!detail.pairs.length)return '<div class="empty">本批没有配成的对照。</div>';
  return `<div class="tablewrap"><table><thead><tr><th>饲喂箱 / 处置</th><th>对照箱</th><th>配对前分值差</th><th>饲喂箱分差</th><th>对照箱分差</th><th>两箱分差之差</th></tr></thead><tbody>${detail.pairs.map(p=>`<tr><td><strong>${text(p.case_hive_code)}</strong><br><span class="code">CARE-${p.care_id}</span><br><span class="code">建单 ${text(p.visit_code)} → 结案后 ${text(p.followup_visit_code)}</span></td><td><strong>${text(p.control_hive_code)}</strong><br><span class="code">${text(p.control_baseline_visit)} → ${text(p.control_followup_visit)}</span></td><td>${num(p.baseline_gap,4)}</td><td>${num(p.case_delta,4)}</td><td>${num(p.control_delta,4)}</td><td><strong>${num(p.diff_in_diff,4)}</strong></td></tr>`).join('')}</tbody></table></div>
  <p class="hint">分差均为“结案后首份巡检分值 − 建单巡检分值”，同一参考重算；两箱分差之差为正表示饲喂箱相对对照箱回升更多。配对前分值差列用于核对两箱起点是否接近。</p>`;
}

function caseList(rows,empty){
  if(!rows.length)return `<div class="empty">${empty}</div>`;
  return `<div class="tablewrap"><table><thead><tr><th>蜂箱 / 处置</th><th>建单巡检</th><th>结案后巡检</th><th>原因</th></tr></thead><tbody>${rows.map(c=>`<tr><td><strong>${text(c.hive_code||hiveName(c.hive_id))}</strong><br><span class="code">CARE-${c.care_id}</span></td><td>${text(c.visit_code||'—')}</td><td>${text(c.followup_visit_code||'—')}</td><td>${text(c.reason||'—')}</td></tr>`).join('')}</tbody></table></div>`;
}

function detailView(){
  const d=state.detail;
  if(!d)return '<div class="empty">选择上方批次查看配对与分差。</div>';
  const b=d.batch;
  return `<section class="sheet" style="margin-top:20px"><div class="care-heading"><div><span class="code">BATCH-${b.id} / 结案 ${text(b.date_from)} 至 ${text(b.date_to)}</span><h3>对照配对与分差 ${statusBadge(b.status)}</h3></div><p class="hint">参考 SHA ${text((b.reference_sha||'').slice(0,12))}<br>阈值 ${num(b.threshold,4)}${b.revisions?`<br>发布前重配 ${b.revisions} 次`:''}${b.published_at?`<br>发布 ${when(b.published_at)}`:''}</p></div>
  ${b.error?`<div class="rule">${text(b.error)}</div>`:''}
  ${b.status==='computed'||b.status==='published'||b.status==='failed'?summaryLine(d):'<div class="empty">批次计算中，稍候自动刷新。</div>'}
  ${detailBody(d)}
  <div class="actions">${account().role==='admin'&&b.status!=='published'&&['computed','failed'].includes(b.status)?`<button class="primary" id="fe-publish">主管发布（材料有变则先重配）</button>`:''}${account().role!=='viewer'&&['failed','computed'].includes(b.status)?'<button id="fe-retry">续跑原批</button>':''}${b.status==='published'?'<p class="hint">已发布：本页保留发布当时的配对与分差依据，后续补录不会改写。</p>':''}</div></section>`;
}

function detailBody(d){
  if(!['computed','published','failed'].includes(d.batch.status))return '';
  return `<h3>配成对照</h3>${pairTable(d)}<div class="two" style="margin-top:20px"><div><h3>缺测（${d.missing.length}）</h3>${caseList(d.missing,'没有缺测单。')}</div><div><h3>没配上（${d.unmatched.length}）</h3>${caseList(d.unmatched,'没有未配上的单。')}</div></div>`;
}

export function render(){
  const range=state.batch?{from:state.batch.date_from,to:state.batch.date_to}:defaultRange();
  return `<section class="sheet" style="margin-top:22px"><h2>饲喂效果对照配对</h2><p class="hint">按结案日期挑选饲喂单，用建单巡检与结案后首份巡检按同一参考重算分差；同场对照箱须两天都有读数且期间未转场、无处置，每批只用一回。先让配成的单尽量多，再让配对前分值尽量接近。</p>
  <form id="fe-form" class="three"><label>结案起始日<input name="date_from" type="date" value="${text(range.from)}" required></label><label>结案截止日<input name="date_to" type="date" value="${text(range.to)}" required></label><label>&nbsp;<button class="primary" ${account().role==='viewer'||state.busy?'disabled':''}>${state.busy?'配对计算中…':'发起对照配对'}</button></label></form>
  <div class="tablewrap"><table><thead><tr><th>批次</th><th>结案区间</th><th>状态</th><th>配成 / 未配 / 缺测</th><th>动作</th></tr></thead><tbody>${book.feedBatches.length?book.feedBatches.map(b=>`<tr><td><strong>BATCH-${b.id}</strong></td><td>${text(b.date_from)} → ${text(b.date_to)}</td><td>${statusBadge(b.status)}</td><td>${b.summary?`${b.summary.paired} / ${b.summary.unmatched} / ${b.summary.missing}`:'—'}</td><td><button data-fe-view="${b.id}">查看配对与分差</button></td></tr>`).join(''):'<tr><td colspan="5">还没有对照配对批次，先选择结案日期发起。</td></tr>'}</tbody></table></div>
  ${detailView()}</section>`;
}

async function loadDetail(id){
  state.batch=book.feedBatches.find(b=>b.id===id)||state.batch;
  state.detail=await api('/feed-effect-batches/'+id);
  return state.detail;
}

async function poll(id,rerender){
  for(let i=0;i<120;i++){
    const d=await loadDetail(id);
    if(['computed','published','failed'].includes(d.batch.status)){rerender();return d;}
    await delay(500);
  }
  rerender();toast('配对仍在计算，可稍后点开该批次查看');
}

export function bind(rerender){
  const form=document.querySelector('#fe-form');
  if(form)form.onsubmit=async e=>{
    e.preventDefault();
    const body=Object.fromEntries(new FormData(e.target));
    if(body.date_from>body.date_to)return toast('结案起始日不能晚于截止日');
    state.busy=true;rerender();
    try{
      const batch=await api('/feed-effect-batches','POST',body);
      state.batch=batch;
      state.detail={batch,cases:[],pairs:[],missing:[],unmatched:[],summary:null};
      book.feedBatches=[batch,...book.feedBatches.filter(b=>b.id!==batch.id)];
      rerender();
      await poll(batch.id,rerender);
    }catch(err){toast(err.message);}finally{state.busy=false;rerender();}
  };
  document.querySelectorAll('[data-fe-view]').forEach(b=>b.onclick=async()=>{
    try{await loadDetail(Number(b.dataset.feView));rerender();}catch(err){toast(err.message);}
  });
  const retry=document.querySelector('#fe-retry');
  if(retry)retry.onclick=async()=>{
    const id=state.detail.batch.id;
    try{await api('/feed-effect-batches/'+id+'/retry','POST',null);state.detail=null;rerender();await poll(id,rerender);}catch(err){toast(err.message);}
  };
  const publish=document.querySelector('#fe-publish');
  if(publish)publish.onclick=async()=>{
    const id=state.detail.batch.id;
    try{
      const result=await api('/feed-effect-batches/'+id+'/publish','POST',null);
      const fresh=await api('/feed-effect-batches');
      book.feedBatches=fresh;
      await loadDetail(id);rerender();
      toast(result.repaired?'材料有变，已重配并发布':'已发布，保留当时配对依据');
    }catch(err){toast(err.message);}
  };
}

