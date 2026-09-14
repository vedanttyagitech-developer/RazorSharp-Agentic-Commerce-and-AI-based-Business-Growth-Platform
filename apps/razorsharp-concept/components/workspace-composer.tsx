'use client';
import {useEffect} from 'react';
import {merchantPrompts} from '@/lib/merchant-prompts';
import {Composer} from './concept';
import {useVoiceSession} from './voice-session';
import './workspace-composer.css';
export function WorkspaceComposer({step,onSend}:{step:'merchant'|'console';onSend?:(text:string)=>void}){
 const voice=useVoiceSession();
  useEffect(()=>{sessionStorage.setItem('razorsharp:tour-step',step)},[step]);
  // Only the step is recorded here. Setting the tour flag as well would divert every later
  // typed turn into the voice tour narration instead of the operations copilot -- the flag
  // means "tour narration active", not "the microphone was once pressed".
  const enter=()=>{sessionStorage.setItem('razorsharp:tour-step',step)};
 return <div className="workspace-composer-dock">{step==='merchant'&&onSend&&<div className="merchant-action-suggestions" aria-label="Try Merchant AI">{merchantPrompts.map(([label,prompt])=><button type="button" key={label} onClick={()=>onSend(prompt)}>{label}</button>)}</div>}<Composer keepExpanded assistantMessage={voice.reply} surface={onSend?'merchant':'shopping'} onStartVoice={()=>{enter();voice.startListening()}} onSend={text=>{if(onSend)onSend(text);else{enter();voice.say(text)}}}/></div>;
}
