'use client';
import {useEffect,useRef,useState} from 'react';
const questions=['Why should Razorpay care?','Is this just a chatbot?','Explain this screen','Show me the proof','How does exact checkout approval work?','How does Merchant Command execute an approved change?','How do you prevent duplicate payments?','How does Reserve Pay use saved permission?','How do ACP and UCP connect to checkout?','How is voice kept separate from payment authority?','What happens if the price changes after approval?','What if payment times out?','Can AI spend without my approval?','How are merchant and buyer sessions separated?','What does the Platform Console show?','How do you verify payment outcomes?','What happens when Gemini is unavailable?','How are Merchant Policy terms preserved?','How do you handle concurrent requests?','What would you measure before scaling?'];
export function EngineeringSuggestions({onPick,onTour,disabled=false}:{disabled?:boolean;onPick:(question:string)=>void;onTour:()=>void}){
 const [index,setIndex]=useState(0);
 const [direction,setDirection]=useState(1);
 const [paused,setPaused]=useState(false);
 const gesture=useRef<{x:number;y:number}|null>(null);
 const swiped=useRef(false);
 const move=(delta:number)=>{if(disabled)return;setDirection(delta);setIndex(value=>(value+delta+questions.length)%questions.length)};
 useEffect(()=>{if(disabled||paused)return;const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');if(reduced.matches)return;const timer=window.setInterval(()=>{if(!document.hidden){setDirection(1);setIndex(value=>(value+1)%questions.length)}},6500);return()=>window.clearInterval(timer)},[disabled,paused,index]);
 return <div className="engineering-suggestion" onMouseEnter={()=>setPaused(true)} onMouseLeave={()=>setPaused(false)} onFocusCapture={()=>setPaused(true)} onBlurCapture={e=>{if(!e.currentTarget.contains(e.relatedTarget))setPaused(false)}}>
  <div className="engineering-question-row">
   <button className="engineering-question" disabled={disabled} type="button"
    onPointerDown={e=>{gesture.current={x:e.clientX,y:e.clientY};swiped.current=false}}
    onPointerUp={e=>{const start=gesture.current;gesture.current=null;if(start&&Math.abs(e.clientX-start.x)>45&&Math.abs(e.clientX-start.x)>Math.abs(e.clientY-start.y)){swiped.current=true;move(e.clientX<start.x?1:-1)}}}
    onPointerCancel={()=>{gesture.current=null;swiped.current=true}}
    onClick={e=>{if(swiped.current&&e.detail!==0){swiped.current=false;return}onPick(questions[index])}}>
    <span key={index} data-direction={direction}>{questions[index]}</span>
   </button>
  </div>
  <button disabled={disabled} type="button" className="composer-tour-button" onClick={onTour}>Take a project tour</button>
 </div>;
}
