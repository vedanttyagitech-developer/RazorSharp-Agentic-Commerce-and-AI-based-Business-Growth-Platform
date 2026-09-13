'use client';
import {useEffect} from 'react';
import {Composer} from './concept';
import {useVoiceSession} from './voice-session';
import './workspace-composer.css';
export function WorkspaceComposer({step,onSend}:{step:'merchant'|'console';onSend?:(text:string)=>void}){
 const voice=useVoiceSession();
 useEffect(()=>{sessionStorage.setItem('razorsharp:tour-step',step)},[step]);
 const enter=()=>{sessionStorage.setItem('razorsharp:tour','on');sessionStorage.setItem('razorsharp:tour-step',step)};
 return <div className="workspace-composer-dock"><Composer keepExpanded assistantMessage={voice.reply} surface={onSend?'merchant':'shopping'} onStartVoice={()=>{enter();voice.startListening()}} onSend={text=>{if(onSend)onSend(text);else{enter();voice.say(text)}}}/></div>;
}
