'use client';
// The headline, with one character occasionally glitching to a random glyph.
//
// It starts RESOLVED and stays readable: the words are the page's own, and a headline that
// assembles itself from noise on arrival is a headline nobody can read while it does. Only
// a single character is ever scrambled, for three frames, on a random interval.
//
// Reduced motion turns it into plain text -- there is nothing to lose, because the resolved
// state is the real one.
import {useEffect,useState} from 'react';

const GLYPHS='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*_+-=|/\\{}■▪·~';

export function ScrambleWord({text,className}:{text:string;className?:string}){
 const [chars,setChars]=useState(()=>text.split(''));
 const [lit,setLit]=useState<number|null>(null);

 const [previousText,setPreviousText]=useState(text);if(previousText!==text){setPreviousText(text);setChars(text.split(''));setLit(null)}
 useEffect(()=>{
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const paused=()=>reduced.matches||document.documentElement.dataset.motion==='paused';
  let step:ReturnType<typeof setInterval>|undefined;
  const idle=setInterval(()=>{
   if(paused()||Math.random()>.18)return;
   const spots=text.split('').map((c,i)=>c!==' '?i:-1).filter(i=>i>=0);
   if(!spots.length)return;
   const at=spots[Math.floor(Math.random()*spots.length)];
   setLit(at);
   let frames=0;
   step=setInterval(()=>{
    frames++;
    setChars(prev=>{const next=[...prev];next[at]=GLYPHS[Math.floor(Math.random()*GLYPHS.length)];return next});
    if(frames>=3){clearInterval(step);setChars(text.split(''));setLit(null)}
   },50);
  },2600);
  return()=>{clearInterval(idle);if(step)clearInterval(step)};
 },[text]);

 return <span className={className}>
  {chars.map((char,i)=><span key={i} className={lit===i?'scramble-lit':undefined}>{char}</span>)}
 </span>;
}

// "Yes this is me, Vedant" -- the answer to the question the page it borrows from asks in
// the same place, in the same type: "Is this you?". Serif italic bookends the line and the
// name takes the emphasis, where the question puts it on "you?".
export function IdentityTitle(){
 return <h1 className="stage-title" id="identity-title">
  <ScrambleWord text="Yes" className="stage-title-serif"/>
  <ScrambleWord text="this" className="stage-title-sans"/>
  <ScrambleWord text="is" className="stage-title-sans"/>
  <ScrambleWord text="me," className="stage-title-sans"/>
  <ScrambleWord text="Vedant" className="stage-title-serif"/>
 </h1>;
}
