'use client';
import {useEffect,useRef} from 'react';

/** Follow new turns and results without pulling a reader away from older messages. */
export function useChatFollow(turn:string,results:string,enabled:boolean){
 const latestRef=useRef<HTMLDivElement>(null);
 const resultsRef=useRef<HTMLElement>(null);
 const following=useRef(true);
 const lastTurn=useRef(turn);
 useEffect(()=>{
  let touchY=0;
  const wheel=(event:WheelEvent)=>{if(event.deltaY<0)following.current=false};
  const key=(event:KeyboardEvent)=>{if(['PageUp','Home','ArrowUp'].includes(event.key)&&!(event.target instanceof HTMLTextAreaElement)&&!(event.target instanceof HTMLInputElement))following.current=false};
  const start=(event:TouchEvent)=>{touchY=event.touches[0]?.clientY??0};
  const move=(event:TouchEvent)=>{if((event.touches[0]?.clientY??touchY)>touchY+8)following.current=false};
  window.addEventListener('wheel',wheel,{passive:true});window.addEventListener('keydown',key);
  window.addEventListener('touchstart',start,{passive:true});window.addEventListener('touchmove',move,{passive:true});
  return()=>{window.removeEventListener('wheel',wheel);window.removeEventListener('keydown',key);window.removeEventListener('touchstart',start);window.removeEventListener('touchmove',move)};
 },[]);
 useEffect(()=>{
  const fresh=lastTurn.current!==turn;lastTurn.current=turn;
  if(fresh)following.current=true;
  if(!enabled||!turn||!following.current)return;
  const frame=requestAnimationFrame(()=>{
   if(!following.current)return;
   const target=(!fresh&&results?resultsRef.current:null)??latestRef.current;
   target?.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
  });
  return()=>cancelAnimationFrame(frame);
 },[turn,results,enabled]);
 return {latestRef,resultsRef};
}
