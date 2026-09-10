'use client';
// The bust, dithered into blue ASCII.
//
// This is the piece the old hero got wrong. It sampled a photograph, then picked each
// glyph at RANDOM and carried the image only in alpha -- so the character grid held no
// picture and the result read as a faint dotted haze rather than a face.
//
// Here the glyph IS the image: luminance runs through an S-curve, a shadow lift and a
// stochastic dither, then indexes a ramp from dense (`Ñ@#W$`) to sparse (`.,_ `). That is
// what makes it read at 6px cells. The subject is a sculpture, not a photograph of a
// person, which is also why it dithers well: hard planes, no skin texture to lose.
import {useEffect,useRef,useState} from 'react';

const WIDTH=782,HEIGHT=838,STEP=6,EDGE_H=50;
const RAMP='Ñ@#W$9876543210?!abc;:+=-,._ ';

export function StatueCanvas(){
 const mainRef=useRef<HTMLCanvasElement>(null),edgeRef=useRef<HTMLCanvasElement>(null);
 const [ready,setReady]=useState(false);

 useEffect(()=>{
  const main=mainRef.current,edge=edgeRef.current;if(!main||!edge)return;
  main.width=WIDTH;main.height=HEIGHT;edge.width=WIDTH;edge.height=EDGE_H;
  const ctx=main.getContext('2d',{alpha:true}),edgeCtx=edge.getContext('2d',{alpha:true});
  if(!ctx||!edgeCtx)return;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const still=()=>reduced.matches||document.documentElement.dataset.motion==='paused';

  const cell=(target:CanvasRenderingContext2D,data:Uint8ClampedArray,x:number,y:number,canvasY:number,noisy:boolean)=>{
   const i=4*(y*WIDTH+x),a=data[i+3];
   if(a<35)return;
   const lum=.299*data[i]+.587*data[i+1]+.114*data[i+2];
   let c=255*((lum/255-.5)*1.15+.5);
   c+=62*(1-Math.max(0,Math.min(255,c))/255);
   c=c*1.55+(noisy?(Math.random()-.5)*120:0);
   c=Math.max(0,Math.min(255,c));
   if(c<12)return;
   const norm=c/255;
   const ch=RAMP[Math.max(0,Math.min(RAMP.length-1,Math.floor((1-Math.pow(norm,.75))*(RAMP.length-1))))];
   // Blue, and it has to stay blue at the top end. The study's curve reached rgb(185,250,255)
   // on a highlight -- near-white cyan -- which washed the bust out against this page's
   // deeper black. Red and green are pulled back and given steeper exponents so only the
   // brightest planes lift, and blue stays dominant across the whole ramp.
   target.fillStyle=`rgb(${Math.round(18+92*Math.pow(norm,1.7))},${Math.round(48+124*Math.pow(norm,1.35))},${Math.round(188+67*norm)})`;
   target.fillText(ch,x+STEP/2,canvasY);
  };

  const face=(target:CanvasRenderingContext2D)=>{
   target.font='bold 7px "JetBrains Mono",Consolas,monospace';
   target.textAlign='center';target.textBaseline='middle';
  };

  const offscreen=document.createElement('canvas');
  offscreen.width=WIDTH;offscreen.height=HEIGHT;
  const off=offscreen.getContext('2d',{willReadFrequently:true});if(!off)return;

  let data:Uint8ClampedArray|null=null,anim=0,lastJitter=0,visible=true;
  const image=new Image();
  image.onload=()=>{
   off.drawImage(image,0,0,WIDTH,HEIGHT);
   data=off.getImageData(0,0,WIDTH,HEIGHT).data;
   ctx.clearRect(0,0,WIDTH,HEIGHT);face(ctx);
   for(let y=0;y<HEIGHT;y+=STEP)for(let x=0;x<WIDTH;x+=STEP)cell(ctx,data,x,y,y+STEP/2,true);
   setReady(true);
   // The living part: four random scanlines re-dither every 80ms, which reads as the
   // surface breathing rather than as an animation loop. Redrawing the whole bust at that
   // rate would cost 18,000 fillText calls per frame for the same impression.
   const live=(now:number)=>{
    if(still())return;
    anim=requestAnimationFrame(live);
    if(!data||!visible||document.hidden||now-lastJitter<=80)return;
    lastJitter=now;face(ctx);
    for(let k=0;k<4;k++){
     const row=Math.floor(Math.random()*HEIGHT/STEP)*STEP;
     for(let x=0;x<WIDTH;x+=STEP){
      if(data[4*(row*WIDTH+x)+3]>=35){ctx.clearRect(x,row,STEP,STEP);cell(ctx,data,x,row,row+STEP/2,true)}
     }
    }
   };
   anim=requestAnimationFrame(live);
   const wake=()=>{cancelAnimationFrame(anim);anim=requestAnimationFrame(live)};
   const observer=new IntersectionObserver(([e])=>{visible=e.isIntersecting;wake()});observer.observe(main);
   addEventListener('razorsharp:motion',wake);document.addEventListener('visibilitychange',wake);reduced.addEventListener('change',wake);
   cleanup=()=>{observer.disconnect();removeEventListener('razorsharp:motion',wake);document.removeEventListener('visibilitychange',wake);reduced.removeEventListener('change',wake)};
  };
  let cleanup=()=>{};
  image.src='/identity-statue.webp';
  return()=>{image.onload=null;cancelAnimationFrame(anim);cleanup()};
 },[]);

 return <div className="statue-layer">
  <canvas ref={mainRef} className="statue" aria-hidden="true" data-ready={ready||undefined}/>
  <canvas ref={edgeRef} className="statue-edge" aria-hidden="true"/>
 </div>;
}
