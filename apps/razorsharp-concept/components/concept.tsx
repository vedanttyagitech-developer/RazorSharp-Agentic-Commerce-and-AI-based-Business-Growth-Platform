'use client';
import './home-composer.css';
import Image from 'next/image';
import Link from 'next/link';
import {EngineeringSuggestions} from './engineering-suggestions';
import {CopilotStatus} from './copilot-status';
import {transition} from './continuity';
import { ArrowUp, Mic, Check, Square, Plus, Minus, ArrowUpRight } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useSurfaceTilt } from '@/components/motion';
import { responseLabels, type ResponsePhase } from '@/components/response-motion';
import { useVoiceSession } from '@/components/voice-session';
import { VoiceWave, type WaveMode } from '@/components/voice-wave';
import { Product,money } from '@/lib/demo';

export function Badge({children,tone='neutral'}:{children:React.ReactNode;tone?:string}){return <span className={`badge ${tone}`}>{children}</span>}
export function Brand(){return <Link href="/" className="wordmark branded-wordmark">razorsharp<span className="platform-brand-label">platform</span></Link>}
export function Primary({children,onClick,disabled=false,className=''}:{children:React.ReactNode;onClick?:()=>void;disabled?:boolean;className?:string}){return <button disabled={disabled} className={`primary ${className}`} onClick={onClick}>{children}</button>}
export function Composer({judgeSuggestions=false,keepExpanded=false,assistantMessage,onStartVoice,onSend,placeholder='Or type what’s on your mind…',compact=false,phase='idle',onStop,visualMode,surface='shopping',showSuggestions=false}:{judgeSuggestions?:boolean;keepExpanded?:boolean;assistantMessage?:string|null;onStartVoice?:()=>void;showSuggestions?:boolean;surface?:'shopping'|'merchant';visualMode?:WaveMode;onSend:(v:string)=>void;placeholder?:string;compact?:boolean;phase?:ResponsePhase;onStop?:()=>void}){
 const inputRef=useRef<HTMLTextAreaElement>(null);
 const tourStarting=useRef(false);
 const pendingQuestion=useRef<string|null>(null);

 const [value,setValue]=useState('');const [editing,setEditing]=useState(false);const voice=useVoiceSession();
 const [requestLocked,setRequestLocked]=useState(false);
 const requestGate=useRef<{reply:string|null;notice:string|null;working:boolean}|null>(null);
 const busy=['thinking','searching','preparing','answering'].includes(phase);
 const requestBusy=busy||voice.phase==='transcribing'||voice.phase==='speaking';
 const blocked=requestLocked||requestBusy;
 const acquireRequest=()=>{if(requestGate.current||requestBusy)return false;requestGate.current={reply:voice.reply,notice:voice.notice,working:false};setRequestLocked(true);return true};
 useEffect(()=>{const gate=requestGate.current;if(!gate)return;if(requestBusy){gate.working=true;return}if(voice.notice&&voice.notice!==gate.notice&&!voice.live){pendingQuestion.current=null;tourStarting.current=false}if(pendingQuestion.current||tourStarting.current)return;if(gate.working||voice.reply!==gate.reply||voice.notice!==gate.notice){requestGate.current=null;setRequestLocked(false)}},[voice,requestBusy]);

 useEffect(()=>{if(pendingQuestion.current&&voice.notice&&!voice.live)pendingQuestion.current=null;if(pendingQuestion.current&&voice.live){const question=pendingQuestion.current;pendingQuestion.current=null;voice.say(question)}if(tourStarting.current&&voice.live){tourStarting.current=false;voice.say(sessionStorage.getItem('razorsharp:tour-resume')==='yes'?'technical walkthrough':'start project tour')}},[voice]);
 useEffect(()=>{const stop=()=>{requestGate.current=null;setRequestLocked(false);pendingQuestion.current=null;tourStarting.current=false;voice.reset()};window.addEventListener('razorsharp:tour-stop',stop);return()=>window.removeEventListener('razorsharp:tour-stop',stop)},[voice]);
 const startTour=()=>{if(!acquireRequest())return;sessionStorage.setItem('razorsharp:tour-explicit','yes');sessionStorage.setItem('razorsharp:tour-resume',sessionStorage.getItem('razorsharp:tour')==='on'?'yes':'no');sessionStorage.setItem('razorsharp:tour','on');if(!sessionStorage.getItem('razorsharp:tour-step'))sessionStorage.setItem('razorsharp:tour-step','merchant');tourStarting.current=true;if(voice.live){tourStarting.current=false;voice.say('technical walkthrough')}else voice.startListening()};
 const explainSuggestion=(question:string)=>{if(!acquireRequest())return;sessionStorage.setItem('razorsharp:tour','on');tourStarting.current=false;if(voice.live){pendingQuestion.current=null;voice.say(question)}else{pendingQuestion.current=question;voice.startListening()}};
 const enterProject=()=>{sessionStorage.setItem('razorsharp:tour','on');if(!sessionStorage.getItem('razorsharp:tour-step'))sessionStorage.setItem('razorsharp:tour-step','merchant')};
 const readyTranscript=surface==='shopping'&&voice.phase==='ready'?voice.transcript:null;const [previousTranscript,setPreviousTranscript]=useState(readyTranscript);if(previousTranscript!==readyTranscript){setPreviousTranscript(readyTranscript);if(readyTranscript!==null)setValue(readyTranscript)}
  // Typed merchant text belongs to the operations copilot unless the merchant is already
  // inside a live voice conversation -- then it continues on that socket, with the merchant
  // surface context the gateway already received. A stale project-tour flag must never divert
  // an operations question into the tour narration: `voice.live` is the conversation state,
  // the flag is not.
  const submit=()=>{if(!blocked&&value.trim()&&(!judgeSuggestions||acquireRequest())){if(judgeSuggestions)enterProject();transition(()=>judgeSuggestions||(surface!=='merchant'&&voice.live)?voice.say(value.trim()):onSend(value.trim()));setValue('');setEditing(false)}};
 const voiceActive=voice.live&&(voice.phase==='listening'||voice.phase==='speaking'||voice.phase==='transcribing');
 const mode:WaveMode=voiceActive?voice.phase as WaveMode:visualMode??(phase==='answering'?'answering':phase==='thinking'||phase==='preparing'?'thinking':phase==='searching'?'searching':'idle');
 const label=voice.phase==='listening'?'Listening':voice.phase==='speaking'?'Speaking':voice.phase==='transcribing'?(voice.live?'Transcribing':'Working on your request'):voice.phase==='ready'?'Ready to send':busy?(compact&&phase==='searching'?'Reading your business':phase==='thinking'?'Thinking':phase==='preparing'?'Preparing your answer':responseLabels[phase]):'Try asking Razor AI';
 const talking=voice.phase==='speaking';
 const formRef=useRef<HTMLFormElement>(null);const [dismissed,setDismissed]=useState(false);
 useEffect(()=>{
  const dismiss=(event:Event)=>{if(formRef.current?.contains(event.target as Node))return;if(event.type==='scroll'&&formRef.current?.contains(document.activeElement))return;setEditing(false);setDismissed(true);inputRef.current?.blur()};
  document.addEventListener('pointerdown',dismiss,true);document.addEventListener('scroll',dismiss,true);document.addEventListener('wheel',dismiss,{capture:true,passive:true});document.addEventListener('touchmove',dismiss,{capture:true,passive:true});
  return()=>{document.removeEventListener('pointerdown',dismiss,true);document.removeEventListener('scroll',dismiss,true);document.removeEventListener('wheel',dismiss,true);document.removeEventListener('touchmove',dismiss,true)};
 },[]);
 const collapsed=!(judgeSuggestions||keepExpanded)&&(dismissed||!(showSuggestions||judgeSuggestions)||phase==='complete'||phase==='stopped')&&!editing&&!busy&&!voiceActive&&voice.phase!=='ready';
 return <form ref={formRef} className={`composer voice-first-composer ${compact?'compact':''} ${busy?'is-working':''} ${voiceActive?'voice-active':''}`} data-phase={mode} data-engaged={editing||!!value.trim()||voiceActive||busy} data-collapsed={collapsed} onSubmit={e=>{e.preventDefault();submit()}}>
   <div className="composer-corner-lights" aria-hidden="true"><i/><i/><i/><i/></div><div className="voice-composer-top"><output className={`voice-state-label ${busy||voice.phase==='transcribing'?'razor-thinking':''}`}><i/>{busy||voice.phase==='transcribing'?'Razor AI is thinking':label}{(busy||voice.phase==='transcribing')&&<span className="razor-thinking-dots" aria-hidden="true"><b/><b/><b/></span>}</output><span className="voice-demo-label">{voice.live?'VOICE CONNECTED':'RAZOR AI'}{surface==='shopping'&&<span className="composer-header-hint">Speak naturally by pressing mic</span>}</span></div>
   <div className="composer-wave-stage"><VoiceWave mode={mode} energy={talking?(voice.spokenWords%3+1)/3:undefined}/>{(judgeSuggestions||keepExpanded||showSuggestions)&&<EngineeringSuggestions disabled={blocked} onPick={explainSuggestion} onTour={startTour}/>}</div>
   {!talking&&!busy&&(voice.phase==='idle'||voice.phase==='listening'||voice.phase==='ready')&&(assistantMessage||(judgeSuggestions&&voice.reply))&&<output className="composer-agent-reply"><span>Razor AI</span><p>{assistantMessage||voice.reply}</p></output>}
   {voiceActive?<div className="inline-voice-transcript" aria-live={talking?'off':'polite'}>{talking?<p>{voice.speech.split(' ').map((w,i)=><span key={i} className={i<voice.spokenWords?'spoken':''}>{w} </span>)}</p>:voice.transcript?<p>{voice.transcript}{voice.phase==='listening'&&<i className="speech-caret"/>}</p>:<p className="voice-hint">{voice.live?'Listening — say what you need.':'Connecting to your voice assistant…'}{voice.phase==='listening'&&<i className="speech-caret"/>}</p>}</div>:null}

   <CopilotStatus/><textarea ref={inputRef} onFocus={()=>{setEditing(true);setDismissed(false)}} onPointerDown={()=>setEditing(true)} onBlur={()=>setEditing(false)} aria-label="Message your copilot" placeholder={voice.phase==='ready'?'Edit your message…':placeholder} rows={1} value={value} onChange={e=>{setValue(e.target.value)}} onKeyDown={e=>{setEditing(true);if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();submit()}}}/>
   <div className="composer-bottom"><span className="composer-context">{voiceActive?(voice.phase==='listening'?'Microphone active · speak naturally':voice.phase==='speaking'?'Assistant speaking':'Waiting for the assistant'):compact?'Your business, in context':'Speak naturally, or type your request.'}</span><div>

    {surface==='merchant'&&<small>Type to your merchant copilot</small>}
    {voice.phase==='listening'?<button type="button" className="talk-button active" aria-label="Stop voice conversation" onClick={voice.finishListening}><Square size={13} fill="currentColor"/><span>Stop voice</span></button>:voice.live&&!voiceActive?<button type="button" className="talk-button" aria-label="Enable microphone" onClick={()=>voice.startListening()}><Mic size={19}/><span>Enable microphone</span></button>:(voiceActive||voice.live)?<button type="button" className="talk-button active" aria-label="Stop voice conversation" onClick={voice.reset}><Square size={13}/><span>Stop voice</span></button>:<button type="button" className="talk-button" onClick={()=>{if(judgeSuggestions){enterProject();voice.startListening()}else if(onStartVoice)onStartVoice();else voice.startListening()}} title="Start a voice conversation" aria-label="Start voice conversation"><Mic size={19}/><span>{compact?'Talk':'Talk to Razor AI'}</span></button>}
   {busy?<button type="button" aria-label="Stop response" className="stop-response" onClick={onStop}><Square size={14} fill="currentColor"/></button>:<button aria-label="Send message" className="send-button" disabled={blocked||!value.trim()}><ArrowUp size={21}/></button>}
   </div></div>
 </form>;
}
/**
 * A product's artwork, or its category glyph when there is none.
 *
 * Eight of the shop's 247 products have a photograph. Before the shelf came from the
 * store, that was invisible -- the eight written down in `lib/demo.ts` were exactly the
 * eight with pictures. Now every product is on the shelf, and a broken image icon on 239
 * of them would read as a broken shop rather than an unphotographed one.
 *
 * The fallback is driven by `onError` rather than by a list of which files exist, because
 * such a list is a third place to keep the same fact and would be wrong the first time
 * somebody added artwork.
 */
export function ProductArt({product,className}:{product:Product;className?:string}){
 const [missing,setMissing]=useState(false);
 // Decorative: the product's name is rendered beside every place this appears, so a
 // labelled image here would have a screen reader say it twice.
 if(missing)return <span className={`product-art-glyph ${className||''}`} aria-hidden="true">{product.symbol}</span>;
 return <Image className={className} src={product.image} alt={product.name} loading="lazy" onError={()=>setMissing(true)} unoptimized width={640} height={640}/>;
}
export function ProductCard({product,onAdd,onRemove,onView,detailOpen=false,quantity=0,disabled=false}:{detailOpen?:boolean;product:Product;onAdd:(source?:HTMLElement)=>boolean|void;onRemove?:(source:HTMLElement)=>void;onView:()=>void;quantity?:number;disabled?:boolean}){
 const tilt=useSurfaceTilt(true);const [added,setAdded]=useState(false);const timer=useRef<ReturnType<typeof setTimeout>|null>(null);
 useEffect(()=>()=>{if(timer.current)clearTimeout(timer.current)},[]);

 return <article data-product={product.id} className={`product-card depth-product ${added?'just-added':''}`} {...tilt}>
  <button className="product-image" style={{background:product.color,viewTransitionName:detailOpen?undefined:`product-${product.id}`}} onClick={onView} aria-label={`View ${product.name}`}><span className="product-pedestal"/><ProductArt product={product}/><span className="product-peek"><ArrowUpRight size={15}/></span></button>
  <div className="product-meta"><button onClick={onView} className="product-name">{product.name}</button><p>{product.unit}</p>{(product.isListed===false||product.isAvailable===false||product.stock===0)&&<small>{product.isListed===false?'Unlisted':'Currently unavailable'}</small>}<div><strong>{money(product.price)}</strong><div className="product-quantity-control" data-filled={quantity>0}>{quantity>0&&onRemove&&<><button type="button" className="product-minus" disabled={disabled} aria-label={`Remove one ${product.name}`} onClick={()=>{if(tilt.ref.current)onRemove(tilt.ref.current)}}><Minus size={14}/></button><span className="product-quantity-value" key={quantity} aria-label={`${quantity} in cart`}>{quantity}</span></>}<button type="button" disabled={disabled||product.isListed===false||product.isAvailable===false||quantity>=product.stock} className={`add-button ${added?'added':''}`} onClick={()=>{if(onAdd(tilt.ref.current??undefined)===false)return;setAdded(true);if(timer.current)clearTimeout(timer.current);timer.current=setTimeout(()=>setAdded(false),650)}} aria-label={`Add one ${product.name}`}><span>{quantity>0?<Plus size={14}/>:added?<Check size={16}/>:<Plus size={16}/>}</span>{quantity===0?'Add':null}</button></div></div></div>
 </article>
}
export function SectionHeading({eyebrow,title,description,action}:{eyebrow?:string;title:string;description?:string;action?:React.ReactNode}){return <div className="section-heading"><div>{eyebrow&&<span className="section-eyebrow">{eyebrow}</span>}<h1>{title}</h1>{description&&<p>{description}</p>}</div>{action}</div>}
export function Empty({title,description,action}:{title:string;description:string;action?:React.ReactNode}){return <div className="empty-state"><ShoppingGlyph/><h3>{title}</h3><p>{description}</p>{action}</div>}
function ShoppingGlyph(){return <div className="empty-glyph"><Plus size={26}/></div>}
