'use client';
import {useEffect,useState} from 'react';
import './project-tour.css';
import {useVoiceSession} from './voice-session';
const routes={merchant:'/merchant/',shopping:'/ecommerce-store/',console:'/platform/'};
type Step=keyof typeof routes;
function openStep(step:Step){const event=new CustomEvent('razorsharp:navigate',{detail:routes[step].replace(/\/$/,''),cancelable:true});if(window.dispatchEvent(event))window.location.assign(routes[step])}
type Guide={reply:string;step:Step;navigate?:Step;sources?:{id:string;title:string;path:string;line?:string;symbol?:string}[]};
export function ProjectTour(){
 const voice=useVoiceSession();
 const navigate=(step:Step)=>{sessionStorage.setItem('razorsharp:tour-step',step);openStep(step)};
 const [guide,setGuide]=useState<Guide|null>(null);
 useEffect(()=>{const receive=(event:Event)=>{const value=(event as CustomEvent<Guide>).detail;if(!value||!(value.step in routes))return;sessionStorage.setItem('razorsharp:tour-step',value.step);setGuide(sessionStorage.getItem('razorsharp:tour-explicit')==='yes'?value:null);if(value.navigate&&value.navigate in routes)openStep(value.navigate)};const stop=()=>setGuide(null);window.addEventListener('razorsharp:tour-stop',stop);window.addEventListener('razorsharp:tour-reply',receive);return()=>{window.removeEventListener('razorsharp:tour-reply',receive);window.removeEventListener('razorsharp:tour-stop',stop)}},[]);
 if(!guide)return null;
 return <aside className="project-tour-panel" aria-label="Project tour"><header><strong>Razor AI · Project tour</strong><button onClick={()=>{sessionStorage.removeItem('razorsharp:tour');sessionStorage.removeItem('razorsharp:tour-explicit');sessionStorage.removeItem('razorsharp:tour-step');voice.reset();setGuide(null);window.dispatchEvent(new Event('razorsharp:tour-stop'))}}>End tour</button></header><p>{guide.reply}</p><nav aria-label="Tour destinations">{Object.entries(routes).map(([step,url])=><a key={step} href={url} onClick={event=>{event.preventDefault();navigate(step as Step)}}>{step==='merchant'?'Merchant Command':step==='shopping'?'Shopping Copilot':'Platform Console'}</a>)}</nav>{guide.navigate&&<a className="primary" href={routes[guide.navigate]} onClick={event=>{event.preventDefault();navigate(guide.navigate!)}}>Open {guide.navigate==='merchant'?'Merchant Command':guide.navigate==='shopping'?'Shopping Copilot':'Platform Console'}</a>}{!!guide.sources?.length&&<details><summary>Engineering sources</summary>{guide.sources.map(source=><p key={source.id}>{source.title} · {source.path}{source.line?`:${source.line}`:''}{source.symbol?` · ${source.symbol}`:''}</p>)}</details>}<button type="button" onClick={()=>voice.say('next')}>Next step</button></aside>
}
