'use client';
// The drifting ASCII field behind the bust.
//
// Ported from the standalone study in ~/Desktop/RAZORPAY ANIMATION (built against
// Razorpay's own AI Builders page). Kept as canvas 2D with no library: the whole thing is
// value noise, a character ramp and one fillText per cell.
//
// It honours reduced motion and the site's own motion toggle, which the study did not have
// to: this hero lives in a scrolling page with a pause control, and a canvas that keeps
// animating behind a paused page is the thing that control exists to stop.
import {useEffect,useRef} from 'react';

const DENSITY='■▪·· ';
const CELL=4,FPS=30;

export function AsciiField(){
 const canvasRef=useRef<HTMLCanvasElement>(null);
 useEffect(()=>{
  const canvas=canvasRef.current;if(!canvas)return;
  const ctx=canvas.getContext('2d',{alpha:true});if(!ctx)return;
  const width=1440,height=750;
  canvas.width=width;canvas.height=height;

  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const still=()=>reduced.matches||document.documentElement.dataset.motion==='paused';

  const hash=(e:number,t:number)=>{const n=43758.5453*Math.sin(127.1*e+311.7*t);return n-Math.floor(n)};
  const smooth=(e:number)=>e*e*(3-2*e);
  const noise2D=(x:number,y:number)=>{
   const ix=Math.floor(x),iy=Math.floor(y),fx=x-ix,fy=y-iy,sx=smooth(fx),sy=smooth(fy);
   return hash(ix,iy)*(1-sx)*(1-sy)+hash(ix+1,iy)*sx*(1-sy)+hash(ix,iy+1)*(1-sx)*sy+hash(ix+1,iy+1)*sx*sy;
  };
  const fbm=(x:number,y:number,octaves:number)=>{
   let val=0,amp=.5,freq=1,total=0;
   for(let i=0;i<octaves;i++){val+=amp*noise2D(x*freq,y*freq);total+=amp;amp*=.5;freq*=2}
   return val/total;
  };
  const smoothstep=(a:number,b:number,v:number)=>{const r=Math.max(0,Math.min(1,(v-a)/(b-a)));return r*r*(3-2*r)};
  const field=(nx:number,ny:number,t:number,cx:number,cy:number)=>{
   const c=1.92*(nx-.5),d=ny-.5,p=c-cx,f=d-cy,ang=Math.atan2(f,p);
   let h=Math.hypot(p,f)/(.34*1.8);
   h+=.1*Math.sin(3*ang+.6*t)+.06*Math.sin(5*ang-.4*t)+.12*Math.sin(2*ang+.25*t);
   h+=.9*(fbm(1.6*p*1.9+.15*t,1.6*f*1.9-.12*t,4)-.5);
   const m=1-smoothstep(.55,1.15,h);
   const v=fbm(3.2*c*1.9-.2*t,3.2*d*1.9+.18*t,5);
   return Math.max(0,Math.min(1,m*(.35+.85*v)));
  };

  let mx=innerWidth/2,my=innerHeight/2,cx=0,cy=0,last=0,anim=0,visible=true;
  const start=performance.now();
  const point=(e:PointerEvent)=>{mx=e.clientX;my=e.clientY};
  addEventListener('pointermove',point,{passive:true});

  const draw=(time:number)=>{
   const frozen=still();
   if(!frozen)anim=requestAnimationFrame(draw);
   if(!visible||document.hidden)return;
   if(time-last<1000/FPS&&!frozen)return;
   last=time;
   const rect=canvas.getBoundingClientRect();
   const tx=1.92*((mx-rect.left)/(rect.width||1)-.5),ty=(my-rect.top)/(rect.height||1)-.5;
   // A frozen field still needs a composition, so it settles on the pointer rather than
   // easing toward it.
   if(frozen){cx=tx;cy=ty}else{const k=1-Math.pow(.95,60/FPS);cx+=(tx-cx)*k;cy+=(ty-cy)*k}
   const elapsed=frozen?0:(time-start)/1000;
   const morph=elapsed*1.5,drift=elapsed*1.5;
   const dx=.7*(.75*Math.sin(.047*drift)+.25*Math.sin(.019*drift));
   const dy=.44*(.74*Math.cos(.039*drift)+.26*Math.sin(.026*drift));
   const ox=cx+dx*.45,oy=cy+dy*.45;

   ctx.clearRect(0,0,width,height);
   ctx.font=`${CELL*.45}px "SF Mono","JetBrains Mono",Menlo,Consolas,monospace`;
   ctx.textAlign='center';ctx.textBaseline='middle';
   const cols=Math.ceil(width/CELL),rows=Math.ceil(height/CELL);
   for(let r=0;r<rows;r++){
    const v=r*CELL+.5*CELL,ny=v/height;
    for(let c=0;c<cols;c++){
     const u=c*CELL+.5*CELL,nx=u/width;
     let j=field(nx,ny,morph,ox,oy);
     if(!frozen)j+=(Math.random()-.5)*.126;
     j=Math.max(0,Math.min(1,j));
     if(j>=.06){
      const ch=DENSITY[Math.floor((1-j)*(DENSITY.length-1))];
      if(ch&&ch!==' '){ctx.fillStyle=`rgba(209,209,209,${(.71*j*j).toFixed(3)})`;ctx.fillText(ch,u,v)}
     }
    }
   }
  };
  const wake=()=>{cancelAnimationFrame(anim);anim=requestAnimationFrame(draw)};
  const observer=new IntersectionObserver(([e])=>{visible=e.isIntersecting;wake()});observer.observe(canvas);
  addEventListener('razorsharp:motion',wake);document.addEventListener('visibilitychange',wake);reduced.addEventListener('change',wake);
  wake();
  return()=>{cancelAnimationFrame(anim);observer.disconnect();removeEventListener('pointermove',point);removeEventListener('razorsharp:motion',wake);document.removeEventListener('visibilitychange',wake);reduced.removeEventListener('change',wake)};
 },[]);
 return <canvas ref={canvasRef} className="ascii-field" aria-hidden="true"/>;
}
