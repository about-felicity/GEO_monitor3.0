"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

const MODELS = [
  ["doubao", "豆包"], ["yuanbao", "腾讯元宝"], ["wenxin", "文心一言"],
  ["quark", "千问"], ["deepseek", "DeepSeek"], ["kimi", "Kimi"],
] as const;
type ModelId = (typeof MODELS)[number][0];
type MonitorTask = { id:string; status:string; completed_steps:number; total_steps:number; message:string; created_at:string; model_progress?:Record<string,{completed:number;total:number}> };
type Monitor = { id:string; customer_slug:string; paid_user_id:string; is_paid:boolean; brand_name:string; product_name:string; question:string; starts_on:string; expires_on:string; status:string; term_state:"upcoming"|"current"|"expired"; model_rounds:Record<ModelId,number>; current_task?:MonitorTask|null; runs:{run_date:string;task:MonitorTask|null}[] };
type Draft = Pick<Monitor,"paid_user_id"|"is_paid"|"brand_name"|"product_name"|"question"|"starts_on"|"expires_on"|"model_rounds">;

const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,10);
const future = (days:number) => { const value = new Date(); value.setDate(value.getDate()+days); return new Date(value.getTime()-value.getTimezoneOffset()*60000).toISOString().slice(0,10); };
const rounds = () => Object.fromEntries(MODELS.map(([id]) => [id,3])) as Record<ModelId,number>;
const emptyDraft = ():Draft => ({ paid_user_id:"",is_paid:true,brand_name:"",product_name:"",question:"",starts_on:today(),expires_on:future(30),model_rounds:rounds() });
function basePath(){ const configured=String(import.meta.env.VITE_MONITOR_BASE_PATH||"").trim().replace(/\/$/,""); if(configured||typeof window==="undefined")return configured; return window.location.pathname.startsWith("/geo")?"/geo":""; }
function taskStatus(value:string){ return ({queued:"排队中",running:"监控中",paused:"已暂停",completed:"今日完成",failed:"待恢复",cancelled:"已停止"} as Record<string,string>)[value]||value; }

export function PaidMonitorCenter(){
  const [items,setItems]=useState<Monitor[]>([]); const [drafts,setDrafts]=useState<Record<string,Draft>>({});
  const [created,setCreated]=useState<Draft>(emptyDraft); const [busy,setBusy]=useState(""); const [error,setError]=useState("");
  const load=useCallback(async()=>{ const response=await fetch(`${basePath()}/api/admin/paid-monitors?_=${Date.now()}`,{credentials:"include",cache:"no-store"}); const body=await response.json(); if(!response.ok||!body.ok)throw Error(body.error||"每日监控读取失败"); const monitors=(body.monitors||[]) as Monitor[]; setItems(monitors); setDrafts(Object.fromEntries(monitors.map(item=>[item.id,{paid_user_id:item.paid_user_id,is_paid:item.is_paid,brand_name:item.brand_name,product_name:item.product_name,question:item.question,starts_on:item.starts_on,expires_on:item.expires_on,model_rounds:{...rounds(),...item.model_rounds}}]))); },[]);
  useEffect(()=>{ void load().catch(reason=>setError(reason.message)); const timer=window.setInterval(()=>void load().catch(()=>{}),3000); return()=>window.clearInterval(timer); },[load]);
  const stats=useMemo(()=>({total:items.length,active:items.filter(x=>x.status==="active").length,running:items.filter(x=>["running","queued"].includes(x.current_task?.status||"")).length,done:items.filter(x=>x.current_task?.status==="completed").length}),[items]);
  async function request(path:string,body?:object){ const response=await fetch(`${basePath()}${path}`,{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):"{}"}); const data=await response.json(); if(!response.ok||!data.ok)throw Error(data.error||"操作失败"); await load(); }
  async function create(event:FormEvent){ event.preventDefault(); setBusy("new");setError("");try{await request("/api/admin/paid-monitors",created);setCreated(emptyDraft());}catch(reason){setError(reason instanceof Error?reason.message:"创建失败");}finally{setBusy("");} }
  async function save(id:string){setBusy(id);setError("");try{await request(`/api/admin/paid-monitors/${id}`,drafts[id]);}catch(reason){setError(reason instanceof Error?reason.message:"保存失败");}finally{setBusy("");}}
  async function action(item:Monitor,name:"pause"|"resume"|"rerun"|"clear"){ if(name==="rerun"&&!window.confirm(`确定重新运行 ${item.paid_user_id} 今天的全部监控吗？`))return; let body:object|undefined; if(name==="clear"){const confirmation=window.prompt(`该操作会清除 ${item.paid_user_id} 的全部监控数据并暂停配置。\n请输入付费用户 ID 确认：`);if(confirmation===null)return;body={confirm_user_id:confirmation};} setBusy(`${item.id}:${name}`);setError("");try{await request(`/api/admin/paid-monitors/${item.id}/${name}`,body);}catch(reason){setError(reason instanceof Error?reason.message:"操作失败");}finally{setBusy("");} }
  function patch(id:string,value:Partial<Draft>){setDrafts(current=>({...current,[id]:{...current[id],...value}}));}
  function fields(value:Draft,set:(next:Partial<Draft>)=>void){return <>
    <label><span>付费用户 ID</span><input required value={value.paid_user_id} onChange={e=>set({paid_user_id:e.target.value})}/></label>
    <label><span>品牌名称</span><input required value={value.brand_name} onChange={e=>set({brand_name:e.target.value})}/></label>
    <label><span>产品名称（可选）</span><input value={value.product_name} onChange={e=>set({product_name:e.target.value})}/></label>
    <label><span>开始日期</span><input required type="date" value={value.starts_on} onChange={e=>set({starts_on:e.target.value})}/></label>
    <label><span>监控期限至</span><input required type="date" value={value.expires_on} onChange={e=>set({expires_on:e.target.value})}/></label>
    <label className="paid-toggle"><span>是否为付费用户</span><input type="checkbox" checked={value.is_paid} onChange={e=>set({is_paid:e.target.checked})}/><b>{value.is_paid?"已开通":"未开通"}</b></label>
    <label className="paid-question"><span>每日需要问的问题（每行一个）</span><textarea required value={value.question} onChange={e=>set({question:e.target.value})}/></label>
    <div className="paid-rounds"><span>每个模型每日轮次</span>{MODELS.map(([id,label])=><label key={id}><b>{label}</b><input type="number" min={1} max={20} value={value.model_rounds[id]} onChange={e=>set({model_rounds:{...value.model_rounds,[id]:Number(e.target.value)}})}/></label>)}</div>
  </>}
  return <section className="paid-monitor-center" id="paid-monitors" aria-label="付费用户每日监控">
    <header><div><small>DAILY CUSTOMER MONITORING</small><h2>付费用户每日监控</h2><p>按北京时间每日自动入队；全局仍保持单活动任务，变更会安全调整后续队列。</p></div><div className="paid-monitor-stats"><span>用户 <b>{stats.total}</b></span><span>启用 <b>{stats.active}</b></span><span>进行中 <b>{stats.running}</b></span><span>今日完成 <b>{stats.done}</b></span></div></header>
    {error&&<div className="diagnosis-error">{error}</div>}
    <form className="paid-monitor-create" onSubmit={create}>{fields(created,value=>setCreated(current=>({...current,...value})))}<button disabled={busy==="new"}>{busy==="new"?"创建中…":"新增付费用户"}</button></form>
    <div className="paid-monitor-list">{items.map(item=>{const draft=drafts[item.id];if(!draft)return null;const task=item.current_task;const percent=task?Math.round(100*task.completed_steps/Math.max(1,task.total_steps)):0;return <article key={item.id} className={item.status!=="active"?"paused":""}>
      <header><div><small>{item.paid_user_id}</small><h3>{item.brand_name}{item.product_name?` · ${item.product_name}`:""}</h3></div><span className={`admin-status ${task?.status||item.status}`}>{task?taskStatus(task.status):item.status!=="active"?"监控已暂停":item.term_state==="upcoming"?"尚未开始":item.term_state==="expired"?"期限已结束":"等待今日任务"}</span></header>
      <div className="paid-progress"><div><i style={{width:`${percent}%`}}/></div><span>{task?`${task.completed_steps}/${task.total_steps} 项 · ${task.message||taskStatus(task.status)}`:"暂无任务"}</span><b>{percent}%</b></div>
      <details><summary>编辑用户配置与模型轮次</summary><div className="paid-monitor-fields">{fields(draft,value=>patch(item.id,value))}</div></details>
      <div className="paid-run-history">{item.runs.slice(0,7).map(run=><span key={run.run_date}>{run.run_date}<b>{run.task?taskStatus(run.task.status):"准备中"}</b></span>)}</div>
      <footer><span>期限 {item.starts_on} 至 {item.expires_on}</span><div><a className="secondary paid-dashboard-link" href={`${basePath()}/${item.customer_slug||`paid-${item.id.slice(0,24)}`}`} target="_blank" rel="noreferrer">打开客户面板</a><button className="secondary" disabled={busy.startsWith(item.id)} onClick={()=>save(item.id)}>保存配置</button><button className="secondary" disabled={busy.startsWith(item.id)} onClick={()=>action(item,item.status==="active"?"pause":"resume")}>{item.status==="active"?"暂停监控":"开始监控"}</button><button className="secondary quota-reset" disabled={busy.startsWith(item.id)} onClick={()=>action(item,"rerun")}>重跑今日</button><button className="danger" disabled={busy.startsWith(item.id)} onClick={()=>action(item,"clear")}>清除数据</button></div></footer>
    </article>})}{!items.length&&<div className="admin-empty"><b>还没有付费监控用户</b><span>新增后，今天的任务会立即进入持久化队列。</span></div>}</div>
  </section>;
}
