'use client';
import {useEffect,useState} from 'react';
const questions=['How does exact checkout approval work?','How does Merchant Command execute an approved change?','How do you prevent duplicate payments?','How does Reserve Pay use saved permission?','How do ACP and UCP connect to checkout?','How is voice kept separate from payment authority?'];
export function EngineeringSuggestions({onPick,onTour,disabled=false}:{disabled?:boolean;onPick:(question:string)=>void;onTour:()=>void}){
 const [index,setIndex]=useState(0);
 useEffect(()=>{if(disabled)return;const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');if(reduced.matches)return;const timer=window.setInterval(()=>{if(!document.hidden)setIndex(value=>(value+1)%questions.length)},6500);return()=>window.clearInterval(timer)},[disabled]);
 return <div className="engineering-suggestion"><button disabled={disabled} type="button" onClick={()=>onPick(questions[index])}><span key={index}>{questions[index]}</span></button><button disabled={disabled} type="button" className="composer-tour-button" onClick={onTour}>Take a project tour</button></div>;
}
