'use client';
import {flushSync} from 'react-dom';
import {useEffect,useState,type Dispatch,type SetStateAction} from 'react';
export const motionAllowed=()=>!matchMedia('(prefers-reduced-motion: reduce)').matches&&document.documentElement.dataset.motion!=='paused';
let updating=false;
let running:{skipTransition:()=>void;ready:Promise<void>;finished:Promise<void>;updateCallbackDone:Promise<void>}|undefined;
export function transition(update:()=>void){
 if(updating||!document.startViewTransition||!motionAllowed()){update();return}
 running?.skipTransition();
 running=document.startViewTransition(()=>{updating=true;try{flushSync(update)}finally{updating=false}});
 // A skipped transition REJECTS `ready`, and skipping is the ordinary outcome here: the
 // line above skips the running one every time a second navigation arrives before the
 // first has finished. Nothing awaited these promises, so each skip surfaced as
 // "Uncaught (in promise) InvalidStateError: Transition was aborted because of invalid
 // state" -- an error overlay on almost every navigation, for the one thing this code
 // deliberately does. The DOM update itself is unaffected; only the animation is dropped.
 //
 // Swallowed rather than logged, and only here: these three promises have exactly one
 // failure mode between them and it is not a fault. `updateCallbackDone` is included
 // because a throw inside `update` rejects it too, and that one is already raised by
 // `flushSync` on the caller's own stack -- reporting it twice would not make it louder.
 const ignore=()=>{};
 running.ready.catch(ignore);running.finished.catch(ignore);running.updateCallbackDone.catch(ignore);
}
export function useMotionState<T>(initial:T):[T,Dispatch<SetStateAction<T>>]{const [value,setValue]=useState(initial);return [value,next=>transition(()=>setValue(next))]}
export function flyToBasket(source:HTMLElement){
 if(!motionAllowed())return;
 const image=source.querySelector('img');const target=document.querySelector('.cart-toggle');if(!image||!target)return;
 const a=image.getBoundingClientRect(),b=target.getBoundingClientRect();const ghost=image.cloneNode(true) as HTMLImageElement;
 ghost.className='flight-thumbnail';ghost.alt='';ghost.setAttribute('aria-hidden','true');Object.assign(ghost.style,{position:'fixed',left:`${a.left}px`,top:`${a.top}px`,width:`${a.width}px`,height:`${a.height}px`,objectFit:'contain',pointerEvents:'none',zIndex:'9999',borderRadius:'18px'});document.body.appendChild(ghost);
 const animation=ghost.animate([{transform:'translate(0,0) scale(1)',opacity:.9},{transform:`translate(${(b.left-a.left)*.5}px,${b.top-a.top-70}px) scale(.6)`,opacity:.85,offset:.55},{transform:`translate(${b.left-a.left}px,${b.top-a.top}px) scale(.08)`,opacity:0}],{duration:650,easing:'cubic-bezier(.22,.7,.2,1)'});
 const clear=()=>{animation.cancel();ghost.remove()};animation.finished.then(()=>ghost.remove(),()=>ghost.remove());setTimeout(clear,900);
}
export function dropFromBasket(source:HTMLElement){
 if(!motionAllowed())return;
 const image=source.querySelector('img');if(!image)return;
 const box=image.getBoundingClientRect();const ghost=image.cloneNode(true) as HTMLImageElement;
 ghost.className='flight-thumbnail';ghost.alt='';ghost.setAttribute('aria-hidden','true');Object.assign(ghost.style,{position:'fixed',left:`${box.left}px`,top:`${box.top}px`,width:`${box.width}px`,height:`${box.height}px`,objectFit:'contain',pointerEvents:'none',zIndex:'9999'});document.body.appendChild(ghost);
 const animation=ghost.animate([{transform:'translate(0,0) rotate(0) scale(.9)',opacity:.85},{transform:'translate(18px,20px) rotate(8deg) scale(.8)',opacity:.65,offset:.3},{transform:'translate(45px,125px) rotate(23deg) scale(.25)',opacity:0}],{duration:480,easing:'cubic-bezier(.4,0,.8,.5)'});
 animation.finished.then(()=>ghost.remove(),()=>ghost.remove());setTimeout(()=>{animation.cancel();ghost.remove()},700);
}
export function MotionPolicy(){useEffect(()=>{const pause=()=>{if(!motionAllowed()){running?.skipTransition();document.getAnimations().forEach(a=>{if(a.effect instanceof KeyframeEffect&&a.effect.target instanceof Element&&a.effect.target.closest('.flight-thumbnail'))a.cancel()})}};window.addEventListener('razorsharp:motion',pause);return()=>window.removeEventListener('razorsharp:motion',pause)},[]);return null}
