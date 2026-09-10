'use client';
// The measurement overlay: bounding boxes, corner dots, `x100` markers, leader lines and
// the green CLICK badge.
//
// Every coordinate is in the stage's own 1440x750 space and is transcribed from the study
// in ~/Desktop/RAZORPAY ANIMATION, so the boxes land on the bust's brow, eye and mouth
// rather than being re-guessed here. The stage scales; these numbers never do.
//
// The LABELS are Razorpay's own, from the AI Builders page this hero answers. That page
// draws this bust, annotates it with these three lines and asks "Is this you?"; this one
// says "Yes this is me". The words only work as a pair -- swapping them for product copy,
// which they were for one commit, left the headline answering a question nobody had asked.
import {useEffect,useState} from 'react';

export type FeatureId='mind'|'eye'|'voice';

type Feature={
 id:FeatureId;
 box:{left:number;top:number;width:number;height:number};
 dots:{left:number;top:number}[];
 markers:{left:number;top:number}[];
 polyline:string;
 text:{left:number;top:number;width:number;label:string};
 origin:string;
};

const FEATURES:Feature[]=[
 {
  id:'mind',
  box:{left:583.32,top:104.4,width:298.81,height:79.31},
  dots:[{left:579.73,top:98.94},{left:878.55,top:98.94},{left:579.73,top:180.13},{left:878.55,top:180.13}],
  markers:[{left:575.2,top:89.26},{left:870.89,top:89.26},{left:575.2,top:196.98},{left:870.89,top:196.98}],
  polyline:'882.16,154.52 1020.65,166.68 1295.06,166.68',
  text:{left:1089,top:180,width:288,label:'ALWAYS THINKING\nHOW TO HARNESS AI'},
  origin:'732.72px 144.06px',
 },
 {
  id:'eye',
  box:{left:665.45,top:267.59,width:92.73,height:81.61},
  dots:[{left:662.95,top:264.27},{left:752.87,top:263.96},{left:663.26,top:343.89},{left:752.87,top:344.2}],
  markers:[{left:657.95,top:258.5},{left:750.99,top:258.5},{left:656.39,top:359.34},{left:750.99,top:359.34}],
  polyline:'757.85,306.27 924.61,391.43 1275.8,391.43',
  text:{left:1041,top:403,width:298,label:'SEES EVERY WORKFLOW\nAS AN AGENT LOOP'},
  origin:'711.82px 308.39px',
 },
 {
  id:'voice',
  box:{left:596.75,top:424.45,width:113.03,height:45.59},
  dots:[{left:593,top:420.7},{left:704.78,top:420.7},{left:593,top:467.84},{left:705.72,top:467.84}],
  markers:[{left:584.26,top:413.99},{left:698.54,top:414.61},{left:584.26,top:481.74},{left:698.54,top:481.74}],
  polyline:'123.27,350.38 508.48,350.38 600.5,454.25',
  text:{left:123,top:288,width:310,label:'SPEAKS IN PROMPTS\nAND GITHUB LINKS'},
  origin:'653.26px 447.24px',
 },
];

const ZONES=[
 {target:'mind' as FeatureId,left:580,top:100,width:305,height:88},
 {target:'eye' as FeatureId,left:662,top:263,width:99,height:90},
 {target:'voice' as FeatureId,left:593,top:420,width:120,height:54},
];

export function FeatureAnnotations({onSelect,hovered,onHover}:{
 onSelect:(id:FeatureId)=>void;hovered:FeatureId|null;onHover:(id:FeatureId|null)=>void;
}){
 const [attention,setAttention]=useState(0);
 // The three take turns lighting up so a still page still shows they are live. It stops
 // the moment a pointer picks one, because two things claiming attention is neither.
 useEffect(()=>{
  if(hovered!==null)return;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  if(reduced.matches)return;
  const id=setInterval(()=>setAttention(p=>(p+1)%FEATURES.length),2400);
  return()=>clearInterval(id);
 },[hovered]);

 return <>
  {FEATURES.map((feat,idx)=>{
   const hot=hovered===feat.id||(hovered===null&&attention===idx);
   const color=hot?'#ffffff':'#7c7c82';
   return <div key={feat.id} className={`feature ${hot?'hot':''}`} style={{transformOrigin:feat.origin,transform:hot?'scale(1)':'scale(.96)',zIndex:hot?8:5}}>
    <div className="feature-box" style={{left:feat.box.left,top:feat.box.top,width:feat.box.width,height:feat.box.height,borderColor:color,background:hot?'rgba(217,217,217,.12)':'rgba(217,217,217,.05)'}}>
     {feat.id==='mind'&&<>
      <span className="click-ripple"/><span className="click-ripple click-ripple-late"/>
      <button type="button" className="click-badge" onClick={e=>{e.stopPropagation();onSelect('mind')}}>CLICK</button>
     </>}
    </div>
    {feat.dots.map((d,i)=><span key={`d${i}`} className="feature-dot" style={{left:d.left,top:d.top,background:color}}/>)}
    {feat.markers.map((m,i)=><span key={`m${i}`} className="feature-marker" style={{left:m.left,top:m.top,color}}>x100</span>)}
    <svg viewBox="0 0 1440 750" preserveAspectRatio="none" className="feature-line">
     <polyline points={feat.polyline} fill="none" stroke={color} strokeWidth={1} strokeLinejoin="round" vectorEffect="non-scaling-stroke"/>
    </svg>
    <div className="feature-text" style={{left:feat.text.left,top:feat.text.top,width:feat.text.width,color}}>{feat.text.label}</div>
   </div>;
  })}
  {ZONES.map(zone=><button
    key={zone.target}
    type="button"
    className="feature-zone"
    aria-label={`Open ${zone.target}`}
    style={{left:zone.left,top:zone.top,width:zone.width,height:zone.height}}
    onClick={e=>{e.stopPropagation();onSelect(zone.target)}}
    onMouseEnter={()=>onHover(zone.target)}
    onMouseLeave={()=>onHover(null)}
    onFocus={()=>onHover(zone.target)}
    onBlur={()=>onHover(null)}
   >{hovered===zone.target&&<span className="feature-tip">click here</span>}</button>)}
 </>;
}
