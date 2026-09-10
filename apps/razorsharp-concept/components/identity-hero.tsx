'use client';
// The identity hero: an ASCII bust on a drifting character field, measured with boxes and
// leader lines, under a scrambling headline.
//
// It is built as a FIXED 1440x750 stage that is scaled to fit, rather than as a responsive
// layout. That is not laziness -- the annotation geometry (every box, dot, `x100` marker
// and leader line) is a set of absolute coordinates that must land on the bust's brow, eye
// and mouth. A responsive layout would move the bust and leave the measurements pointing
// at nothing. One transform keeps the whole composition honest at any size.
//
// Replaces an earlier hero that dithered a PHOTOGRAPH OF A PERSON. The subject is now a
// sculpture; the photograph and its file are gone.
import {useCallback,useEffect,useState} from 'react';
import {ArrowDown,ArrowUpRight,AudioLines,Command} from 'lucide-react';
import {AsciiField} from './ascii-field';
import {StatueCanvas} from './statue-canvas';
import {IdentityTitle} from './scramble-title';
import {FeatureAnnotations,type FeatureId} from './feature-annotations';
import {PossibilityScene} from './possibility-scene';

const STAGE_W=1440,STAGE_H=750;

export function IdentityHero(){
 const [scale,setScale]=useState(1);
 const [hovered,setHovered]=useState<FeatureId|null>(null);
 const [open,setOpen]=useState(false);
 const [connected,setConnected]=useState(false);

 // Contain, not cover: the stage is never cropped, because a cropped stage loses the
 // annotations at the edges, which are the half that explains the picture.
 useEffect(()=>{
  const fit=()=>setScale(Math.min(innerWidth/STAGE_W,Math.min(innerHeight,STAGE_H+120)/STAGE_H));
  fit();addEventListener('resize',fit);return()=>removeEventListener('resize',fit);
 },[]);

 const select=useCallback((_id:FeatureId)=>{setConnected(true);setOpen(true)},[]);

 return <section className="identity-hero" aria-labelledby="identity-title">
  {/* `transform: scale()` does NOT change an element's layout box: the stage still
      occupies 1440x750 however small it is drawn, which left a tall band of dead space
      above and below it. The wrapper is given the SCALED height instead, and the stage
      scales from its top edge so the two agree. */}
  <div className="stage-fit" style={{height:`calc(750px * ${scale})`}}>
   <div className="identity-stage" style={{transform:`scale(${scale})`}}>
    <div className="stage-grid" aria-hidden="true"/>
    <AsciiField/>
    <div className="stage-vignette" aria-hidden="true"/>

    <div className="stage-nav">
     <span className="stage-brand"><i/> HUMAN FIRST. INTELLIGENCE ALONGSIDE.</span>
     <span className="stage-role">RAZORSHARP / 001</span>
    </div>

    <StatueCanvas/>
    <IdentityTitle/>
    <FeatureAnnotations onSelect={select} hovered={hovered} onHover={setHovered}/>
   </div>
  </div>

  {/* Below the stage, not inside it. Anything placed in the stage is scaled with it, and
      at the widths this page actually renders that put this line at about five pixels tall,
      tucked under the headline's descenders. The stage is the picture; these are the page. */}
  <div className="identity-foot">
   <p className="stage-whisper">{connected?'A little more possibility. Still entirely you.':'Your taste. Your ambition. Your way.'}</p>
   {connected&&<div className="identity-destinations">
    <a href="/shop"><AudioLines size={15}/> My shopping copilot <ArrowUpRight size={14}/></a>
    <a href="/merchant"><Command size={15}/> My business copilot <ArrowUpRight size={14}/></a>
   </div>}
   <a className="identity-scroll" href="#copilots"><span>AND THIS IS WHAT’S NEXT</span><ArrowDown size={18}/></a>
  </div>
  <PossibilityScene open={open} onOpenChange={setOpen}/>
 </section>;
}
