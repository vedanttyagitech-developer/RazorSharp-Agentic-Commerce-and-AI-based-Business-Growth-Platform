'use client';
import {useState} from 'react';
import {Dialog,DialogContent,DialogTitle,DialogDescription} from '@/components/ui/dialog';
import {VoiceWave} from './voice-wave';
import {ArrowUpRight,Check,AudioLines} from 'lucide-react';
export function PossibilityScene({open,onOpenChange}:{open:boolean;onOpenChange:(open:boolean)=>void}){
 const [depth,setDepth]=useState(0);
 return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="possibility-dialog"><div className="possibility-heading"><span>RAZORSHARP / THROUGH YOUR EYES</span><DialogTitle>A thought. A whole new world.</DialogTitle><DialogDescription>Explore two copilots in this interactive concept. All examples are simulated.</DialogDescription></div><div className="possibility-stage" style={{'--depth':depth} as React.CSSProperties}>
 <article className="space-window window-shop"><header><i/> YOUR EVERYDAY <span>01</span></header><p>“Breakfast for two,<br/>under ₹500.”</p><div className="space-products">{['AMUL-DAIRY-001','BANA-PROD-006','BRIT-BAKE-001'].map(p=><img key={p} src={`/products/${p}.webp`} alt={p} onError={e=>{e.currentTarget.style.display='none'}}/>)}</div><a href="/shop">Find your everyday <ArrowUpRight size={16}/></a></article>
 <article className="space-window window-voice"><header><AudioLines size={14}/> SPEAK YOUR MIND</header><VoiceWave mode="speaking"/><p>Less typing.<br/><em>More being understood.</em></p><span>Visual voice preview</span></article>
 <article className="space-window window-growth"><header><i/> MERCHANT COMMAND <span>02</span></header><p>Make your next<br/><em>big move.</em></p><div className="space-bars">{[30,48,39,65,54,78,71,95].map((v,i)=><i key={i} style={{height:v+'%',animationDelay:i*.1+'s'}}/>)}</div><a href="/merchant">Explore your business <ArrowUpRight size={16}/></a></article>
 <article className="space-window window-control"><header>INTELLIGENCE, WITH INTENTION</header>{['A thoughtful proposal','Your explicit approval','A traceable outcome'].map(t=><p key={t}><Check size={15}/>{t}</p>)}<small>Preview of the intended experience</small></article>
 <div className="space-orbit" aria-hidden="true"/></div><div className="possibility-controls"><label htmlFor="scene-depth">EXPLORE THE DEPTH</label><input id="scene-depth" type="range" min="0" max="100" value={depth} onChange={e=>setDepth(Number(e.target.value))}/><span>Drag to move through the scene</span></div></DialogContent></Dialog>;
}
