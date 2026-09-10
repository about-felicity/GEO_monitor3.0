"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { EnterpriseBrandLockup } from "./EnterpriseBrand";

const MODELS = [["doubao","豆包"],["yuanbao","腾讯元宝"],["wenxin","文心一言"],["quark","千问"],["deepseek","DeepSeek"],["kimi","Kimi"]] as const;
type ModelMetric = { id:string; completed:number; target_rounds:number; recommendation_rate:number; first_share:number; top3_share:number; top5_share:number };
type Competitor = { name:string; visibility_score:number; mention_rounds:number; recommended_mentions:number; models:string[]; products:string[]; active_days:number; model_visibility?:Record<string,number> };
type Source = { url:string; title:string; domain:string; citation_count:number; models:string[]; active_days:number };
type Day = { date:string; status:string; completed_steps:number; total_steps:number; updated_at:string; overall_rate:number; first_share:number; top3_share:number; top5_share:number; recommended_rounds:number; completed_rounds:number; target_rounds:number; models:ModelMetric[]; competitors:Competitor[]; sources:Source[]; source_analysis:{total_citations?:number;unique_links?:number;unique_domains?:number} };
type Payload = { monitor:{paid_user_id:string;brand_name:string;product_name:string;questions:string[];starts_on:string;expires_on:string;status:string;model_rounds:Record<string,number>;updated_at:string}; current_task?:{status:string;completed_steps:number;total_steps:number;message:string;updated_at:string}|null; days:Day[]; latest:Day|null; competitors:Competitor[]; sources:Source[] };
type View = "overview"|"trend"|"competitors"|"sources";

function basePath(){ const value=String(import.meta.env.VITE_MONITOR_BASE_PATH||"").trim().replace(/\/$/,""); if(value||typeof window==="undefined")return value;return window.location.pathname.startsWith("/geo")?"/geo":""; }
function pct(value:number|undefined){return `${Number(value||0).toFixed(1).replace(".0","")}%`;}
function statusText(value:string){return ({queued:"等待监控",running:"今日监控中",paused:"监控已暂停",completed:"今日已归档",failed:"等待自动恢复",cancelled:"本轮已停止"} as Record<string,string>)[value]||"等待监控";}
function formatDate(value:string){const parts=value.split("-");return parts.length===3?`${Number(parts[1])}/${Number(parts[2])}`:value;}

function TrendChart({days,platform,competitors}:{days:Day[];platform:string;competitors:Competitor[]}){
  const series=useMemo(()=>{
    const top=competitors.slice(0,2);
    return [{name:"监测品牌",color:"#4f7cff",values:days.map(day=>platform==="all"?day.overall_rate:Number(day.models.find(x=>x.id===platform)?.recommendation_rate||0))},...top.map((item,index)=>({name:item.name,color:index===0?"#7d8da8":"#a9b5c8",values:days.map(day=>{const found=day.competitors.find(x=>x.name===item.name);return platform==="all"?Number(found?.visibility_score||0):Number(found?.model_visibility?.[platform]||0);})}))];
  },[days,platform,competitors]);
  const width=760,height=270,left=42,right=18,top=18,bottom=42,plotW=width-left-right,plotH=height-top-bottom;
  const x=(i:number)=>left+(days.length<=1?plotW/2:i*plotW/(days.length-1));
  const y=(value:number)=>top+(100-Math.max(0,Math.min(100,value)))*plotH/100;
  if(!days.length)return <div className="monitor-empty"><b>等待首日数据</b><span>今日监控完成一轮后，趋势会自动出现。</span></div>;
  return <div className="monitor-chart"><div className="monitor-chart-key">{series.map((item,index)=><span key={item.name} className={index===0?"target":""}><i style={{background:item.color}}/>{item.name}</span>)}</div><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="每日推荐概率趋势">
    {[0,25,50,75,100].map(value=><g key={value}><line x1={left} x2={width-right} y1={y(value)} y2={y(value)} /><text x={left-10} y={y(value)+4} textAnchor="end">{value}%</text></g>)}
    {days.map((day,index)=><text key={day.date} x={x(index)} y={height-13} textAnchor="middle">{formatDate(day.date)}</text>)}
    {series.map((item,index)=>{const points=item.values.map((value,i)=>`${x(i)},${y(value)}`).join(" ");return <g key={item.name}><polyline className={index===0?"primary":"secondary"} style={{stroke:item.color}} points={points}/>{item.values.map((value,i)=><circle key={i} cx={x(i)} cy={y(value)} r={index===0?4:3} style={{fill:item.color}}><title>{dayLabel(days[i])} · {item.name} {pct(value)}</title></circle>)}</g>})}
  </svg></div>;
}
function dayLabel(day:Day){return `${day.date}${day.status!=="completed"?` · ${statusText(day.status)}`:""}`;}

export function PaidCustomerDashboard({customerSlug}:{customerSlug:string}){
  const [data,setData]=useState<Payload|null>(null);const [error,setError]=useState("");const [view,setView]=useState<View>("overview");const [platform,setPlatform]=useState("all");
  const load=useCallback(async()=>{const response=await fetch(`${basePath()}/api/paid-monitor/${customerSlug}?_=${Date.now()}`,{cache:"no-store",credentials:"include"});const body=await response.json();if(!response.ok||!body.ok)throw Error(body.error||"监控面板读取失败");setData(body);setError("");},[customerSlug]);
  useEffect(()=>{void load().catch(reason=>setError(reason.message));const timer=window.setInterval(()=>void load().catch(()=>{}),5000);return()=>window.clearInterval(timer);},[load]);
  const visibleDays=useMemo(()=>data?.days.filter(day=>day.completed_rounds>0)||[],[data]);
  const latest=data?.latest;const monitor=data?.monitor;const current=data?.current_task;const progress=current?Math.round(100*current.completed_steps/Math.max(1,current.total_steps)):0;
  const filteredCompetitors=useMemo(()=>{if(!data)return[];if(platform==="all")return data.competitors;const index=new Map<string,{item:Competitor,total:number,days:number}>();visibleDays.forEach(day=>day.competitors.forEach(item=>{const score=Number(item.model_visibility?.[platform]||0);if(!score)return;const old=index.get(item.name)||{item,total:0,days:0};old.total+=score;old.days+=1;index.set(item.name,old);}));return [...index.values()].map(x=>({...x.item,visibility_score:Number((x.total/Math.max(1,visibleDays.length)).toFixed(1))})).sort((a,b)=>b.visibility_score-a.visibility_score);},[data,platform,visibleDays]);
  const filteredSources=useMemo(()=>data?.sources.filter(item=>platform==="all"||item.models.includes(platform))||[],[data,platform]);
  if(error&&!data)return <main className="monitor-fatal"><EnterpriseBrandLockup/><h1>监控面板暂不可用</h1><p>{error}</p><button onClick={()=>void load()}>重新连接</button></main>;
  if(!monitor)return <main className="monitor-fatal"><EnterpriseBrandLockup/><span className="monitor-loader"/><p>正在载入每日监控数据…</p></main>;
  const modelMetric=(id:string)=>latest?.models.find(item=>item.id===id);
  return <div className="monitor-dashboard-shell">
    <aside className="monitor-dashboard-sidebar"><EnterpriseBrandLockup compact/><div className="monitor-customer"><small>持续监控品牌</small><b>{monitor.brand_name}</b><span>{monitor.product_name||"全品牌产品线"}</span></div><nav aria-label="监控报表导航">{([['overview','监测概览','⌂'],['trend','概率趋势','↗'],['competitors','竞品分析','◇'],['sources','信源追踪','⌁']] as [View,string,string][]).map(([id,label,icon])=><button key={id} className={view===id?"active":""} onClick={()=>{setView(id);window.scrollTo({top:0,behavior:"smooth"});}}><i>{icon}</i>{label}</button>)}</nav><div className="monitor-side-status"><i className={current?.status==="running"?"live":""}/><span><b>{statusText(current?.status||monitor.status)}</b><small>{visibleDays.length} 个数据日已归档</small></span></div></aside>
    <div className="monitor-dashboard-workspace"><header className="monitor-dashboard-topbar"><div><small>GEO INTELLIGENCE / DAILY MONITOR</small><h1>AI 品牌推荐每日监控</h1><p>{monitor.brand_name}{monitor.product_name?` · ${monitor.product_name}`:""}</p></div><div><span>监控周期</span><b>{monitor.starts_on} — {monitor.expires_on}</b></div></header>
    <main className="monitor-dashboard-main">
      {current&&["queued","running","paused","failed"].includes(current.status)&&<section className={`monitor-live-task ${current.status}`}><div><i/><span><small>{statusText(current.status)}</small><b>{current.message||"每日数据正在按轮采集"}</b></span></div><strong>{progress}%</strong><span className="bar"><i style={{width:`${progress}%`}}/></span><em>{current.completed_steps}/{current.total_steps} 项 · 已完成数据实时保留</em></section>}
      {view==="overview"&&<div className="monitor-view"><section className="monitor-query-card"><span>Q</span><div><small>每日监测问题</small><b>{monitor.questions[0]}</b><em>{monitor.questions.length>1?`共 ${monitor.questions.length} 个问题 · 每轮逐条采集并归档`:"六模型逐轮采集并归档"}</em></div><div><small>最新数据日</small><b>{latest?.date||"等待采集"}</b><em>{latest?`${latest.completed_rounds}/${latest.target_rounds} 个独立样本`:"数据产生后自动更新"}</em></div></section>
        <section><Header title="数据指标" note="采用与诊断报告一致的可审计计算口径"/><div className="monitor-kpis"><Kpi label="综合推荐概率" value={pct(latest?.overall_rate)} note="六模型等权综合" primary/><Kpi label="首位占比" value={pct(latest?.first_share)} note="排名第 1 的保守占比"/><Kpi label="Top 3 占比" value={pct(latest?.top3_share)} note="进入前三的保守占比"/><Kpi label="有效监控天数" value={`${visibleDays.length} 天`} note={`最近更新 ${latest?.date||"—"}`}/><Kpi label="竞品品牌" value={`${data.competitors.length} 个`} note="历史回答去重识别"/><Kpi label="真实信源" value={`${data.sources.length} 条`} note="历史引用链接去重"/></div></section>
        <section><Header title="六模型最新表现" note={latest?`${latest.date} · 每个平台独立计算`:"等待首轮数据"}/><div className="monitor-model-grid">{MODELS.map(([id,name])=>{const item=modelMetric(id);return <article key={id}><span>{name}</span><b>{pct(item?.recommendation_rate)}</b><em>{item?`${item.completed}/${item.target_rounds} 轮完成`:"等待数据"}<i><u style={{width:`${item?100*item.completed/Math.max(1,item.target_rounds):0}%`}}/></i></em></article>})}</div></section>
      </div>}
      {view==="trend"&&<div className="monitor-view"><Header title="综合推荐概率趋势" note={`${visibleDays[0]?.date||"—"} 至 ${visibleDays.at(-1)?.date||"—"} · 每日真实采样`}/><PlatformFilter value={platform} set={setPlatform}/><section className="monitor-panel"><TrendChart days={visibleDays} platform={platform} competitors={data.competitors}/><div className="monitor-trend-days">{visibleDays.slice(-7).map(day=><span key={day.date}><small>{day.date}</small><b>{pct(platform==="all"?day.overall_rate:day.models.find(x=>x.id===platform)?.recommendation_rate)}</b><em>{day.completed_rounds} 个有效样本</em></span>)}</div></section></div>}
      {view==="competitors"&&<div className="monitor-view"><Header title="竞品分析" note="从每日模型回答正文与结构化排名中识别"/><PlatformFilter value={platform} set={setPlatform}/><section className="monitor-panel monitor-list">{filteredCompetitors.map((item,index)=><article key={item.name}><span>{String(index+1).padStart(2,"0")}</span><div><b>{item.name}</b><small>{item.products.slice(0,3).join(" · ")||`${item.active_days} 天出现`}</small></div><div className="meter"><i style={{width:`${Math.min(100,item.visibility_score)}%`}}/></div><strong>{pct(item.visibility_score)}</strong><em>{item.mention_rounds} 次提及</em></article>)}{!filteredCompetitors.length&&<Empty text="当前平台尚未识别到有效竞品"/>}</section></div>}
      {view==="sources"&&<div className="monitor-view"><Header title="真实信源追踪" note="链接按历史引用次数聚合，可直接核查"/><PlatformFilter value={platform} set={setPlatform}/><div className="monitor-source-summary"><span><small>累计引用</small><b>{filteredSources.reduce((sum,x)=>sum+x.citation_count,0)}</b></span><span><small>去重链接</small><b>{filteredSources.length}</b></span><span><small>来源域名</small><b>{new Set(filteredSources.map(x=>x.domain)).size}</b></span></div><section className="monitor-panel monitor-source-list">{filteredSources.map((item,index)=><a href={item.url} target="_blank" rel="noreferrer" key={item.url}><span>{String(index+1).padStart(2,"0")}</span><div><b>{item.title}</b><small>{item.domain} · {item.models.map(id=>MODELS.find(x=>x[0]===id)?.[1]||id).join("、")}</small></div><em>{item.citation_count} 次引用</em><strong>↗</strong></a>)}{!filteredSources.length&&<Empty text="当前平台尚未采集到可验证信源"/>}</section></div>}
    </main></div>
  </div>;
}

function Header({title,note}:{title:string;note:string}){return <header className="monitor-section-header"><div><small>DAILY INSIGHT</small><h2>{title}</h2></div><p>{note}</p></header>}
function Kpi({label,value,note,primary=false}:{label:string;value:string;note:string;primary?:boolean}){return <article className={primary?"primary":""}><small>{label}</small><b>{value}</b><span>{note}</span></article>}
function PlatformFilter({value,set}:{value:string;set:(value:string)=>void}){return <div className="monitor-platform-filter"><button className={value==="all"?"active":""} onClick={()=>set("all")}>全部模型</button>{MODELS.map(([id,name])=><button key={id} className={value===id?"active":""} onClick={()=>set(id)}>{name}</button>)}</div>}
function Empty({text}:{text:string}){return <div className="monitor-empty"><b>暂无数据</b><span>{text}</span></div>}
