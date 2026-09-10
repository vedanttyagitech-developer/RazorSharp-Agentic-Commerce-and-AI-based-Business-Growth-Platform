'use client';

import { useCallback,useEffect,useRef,useState } from 'react';
import { Check,CirclePause,Search,Sparkles,Square,Volume2 } from 'lucide-react';
import { useVoiceSession } from '@/components/voice-session';

export type ResponsePhase = 'idle' | 'thinking' | 'searching' | 'preparing' | 'answering' | 'complete' | 'stopped';
export const responseLabels: Record<ResponsePhase,string> = {
  idle:'Your everyday, understood',thinking:'Understanding your request',searching:'Searching the sample catalogue',preparing:'Putting your answer together',answering:'Answering',complete:'Ready when you are',stopped:'Response stopped',
};

/** Cancellable choreography for this mock. It does not represent model execution. */
export function useDemoResponse() {
  const [phase,setPhase]=useState<ResponsePhase>('idle');
  const [hasAnswer,setHasAnswer]=useState(false);
  const timers=useRef<ReturnType<typeof setTimeout>[]>([]);
  const clear=useCallback(()=>{timers.current.forEach(clearTimeout);timers.current=[]},[]);
  useEffect(()=>clear,[clear]);
  const start=useCallback(()=>{
    clear();setHasAnswer(false);setPhase('thinking');
    timers.current=[setTimeout(()=>setPhase('searching'),800),setTimeout(()=>setPhase('preparing'),1950),setTimeout(()=>{setHasAnswer(true);setPhase('answering')},2500),setTimeout(()=>setPhase('complete'),4300)];
  },[clear]);
  const stop=useCallback(()=>{clear();setPhase('stopped')},[clear]);
  const reset=useCallback(()=>{clear();setHasAnswer(false);setPhase('idle')},[clear]);
  return {phase,hasAnswer,start,stop,reset,busy:['thinking','searching','preparing','answering'].includes(phase)};
}

export function ResponseActivity({phase,merchant=false,live=false}:{phase:ResponsePhase;merchant?:boolean;live?:boolean}) {
  if(phase==='stopped')return <div className="response-stopped" role="status"><CirclePause size={17}/><span>Stopped. You can send another message anytime.</span></div>;
  const index=['thinking','searching','preparing'].indexOf(phase);
  if(index<0)return null;
  const labels=['Understanding your request',merchant?'Reading sample business records':'Searching the sample catalogue','Putting your answer together'];
  return <div className={`response-activity ${phase}`}>
    <div className="activity-orbit" aria-hidden="true">{phase==='searching'?<Search size={21}/>:<Sparkles size={21}/>}<i/><i/></div>
    <div className="activity-content"><div className="activity-heading" role="status" aria-live="polite"><span key={phase}>{labels[index]}</span><span className="activity-dots" aria-hidden="true"><i/><i/><i/></span></div><p className="activity-disclaimer">{live?'Waiting for the assistant · products come from store records':'Interaction preview · no live model call'}</p>
    <div className="activity-trail" aria-hidden="true">{['Understand',merchant?'Read records':'Find products','Present'].map((label,i)=><span key={label} className={i<index?'done':i===index?'current':''}>{i<index?<Check size={12}/>:<i/>}{label}</span>)}</div>
    {phase==='searching'&&<div className="search-scan" aria-hidden="true"><div/><div/><div/><span/></div>}</div>
  </div>;
}

export function ResponseVoice({text}:{text:string}) {
  const voice=useVoiceSession();
  const active=voice.phase==='speaking'&&voice.speech===text;
  return <button className={`response-voice ${active?'active':''}`} onClick={()=>active?voice.reset():voice.speak(text)} aria-label={active?'Stop voice preview':'Preview spoken response'}>{active?<Square size={13}/>:<Volume2 size={15}/>}<span>{active?'Speaking · stop':'Hear this response'}</span></button>;
}

export function AnswerText({text,animate=false}:{text:string;animate?:boolean}){
 return <p className={animate?'answer-text revealing':'answer-text'}><span className="sr-only">{text}</span><span aria-hidden="true">{text.split(' ').map((word,i)=><span key={i} style={{animationDelay:Math.min(i*.025,1)+'s'}}>{word} </span>)}</span></p>;
}
