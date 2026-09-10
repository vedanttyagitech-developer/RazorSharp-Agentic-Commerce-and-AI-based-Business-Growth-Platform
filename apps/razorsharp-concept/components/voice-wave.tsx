'use client';

import { useEffect, useRef } from 'react';

export type WaveMode='idle'|'listening'|'transcribing'|'thinking'|'searching'|'answering'|'speaking'|'ready';
const palettes:Record<WaveMode,[string,string,string]>={
  idle:['#6988ed','#79b4f4','#bab2f5'],listening:['#078eaa','#40cbd0','#72acd8'],
  transcribing:['#b58222','#eac168','#e7a67b'],thinking:['#bd8627','#eac074','#e5a688'],
  searching:['#438ac8','#79d0f0','#9cbce9'],answering:['#198d70','#68c4a3','#86cdb8'],
  speaking:['#7854d9','#b193f1','#6e9de9'],ready:['#6988ed','#79b4f4','#bab2f5'],
};

/** An illustrative voice ribbon, not an audio-level measurement. */
export function VoiceWave({mode='idle',className='',energy}:{mode?:WaveMode;className?:string;energy?:number}){
  const canvasRef=useRef<HTMLCanvasElement>(null);
  const modeRef=useRef(mode);const energyRef=useRef(energy);
  useEffect(()=>{energyRef.current=energy},[energy]);
  useEffect(()=>{modeRef.current=mode;canvasRef.current?.dispatchEvent(new Event('wave:update'))},[mode]);
  useEffect(()=>{
    const canvas=canvasRef.current;if(!canvas)return;
    const ctx=canvas.getContext('2d');if(!ctx)return;
    const reduced=matchMedia('(prefers-reduced-motion: reduce)');
    let frame=0,resizeFrame=0,last=0,t=0,visible=true,w=400,h=70,amplitude=8,pointer=.5,hover=0,hoverTarget=0,kick=0;
    const still=()=>reduced.matches||document.documentElement.dataset.motion==='paused';
    function render(now:number){
      frame=0;if(!visible||document.hidden)return;
      if(now-last>=32||still()){
        hover+=(hoverTarget-hover)*.12;kick*=.9;
        const current=modeRef.current;
        const active=current==='speaking'||current==='listening';
        const target=(active?.32:current==='answering'?.2:current==='idle'||current==='ready'?.1:.14)*h;
        const level=energyRef.current;
        amplitude+=((level===undefined?target:target*(.45+Math.max(0,Math.min(1,level))*.75))-amplitude)*.1;
        if(!still())t+=Math.min((now-last)/1000,.05)*(current==='speaking'?1.15:.8);
        last=now;ctx!.clearRect(0,0,w,h);
        const colors=palettes[current];
        const bounce=still()?1:1+Math.sin(t*1.4)*.045+kick*.06;
        amplitude=Math.min(amplitude,h*.32);
        // A single coherent ribbon: paired contours share a smooth envelope.
        // No equalizer bars, particles or competing animation layers.
        const sample=(x:number,offset:number)=>{
          const envelope=Math.pow(Math.sin(Math.PI*x),2.1);
          const sway=still()?0:t;
          const primary=Math.sin(x*Math.PI*4-sway*1.5+offset);
          const secondary=Math.sin(x*Math.PI*6+sway*.7+offset*.6)*.16;
          const local=still()?0:hover*Math.exp(-Math.pow((x-pointer)*5,2))*.12;
          return h/2+(primary+secondary)*amplitude*envelope*(.9+local)*bounce;
        };
        const gradient=ctx!.createLinearGradient(0,0,w,0);
        gradient.addColorStop(0,colors[0]+'00');
        gradient.addColorStop(.22,colors[0]);gradient.addColorStop(.5,colors[1]);
        gradient.addColorStop(.78,colors[2]);gradient.addColorStop(1,colors[2]+'00');
        const dualListening=current==='listening'&&!canvas!.closest('.reserve-workspace,.reserve-inline-checkout,.reserve-entry-loader')&&!canvas!.closest('.checkout-shell')?.querySelector('.reserve-inline-checkout,.reserve-entry-loader');
        if(dualListening){
          // Two tightly layered ribbons; colour belongs to the contours, not a backdrop.
          const listeningSample=(x:number,offset:number)=>{
            const envelope=Math.pow(Math.sin(Math.PI*x),1.65);
            const time=still()?0:t;
            const wave=Math.sin(x*Math.PI*7-time*1.65+offset);
            const detail=Math.sin(x*Math.PI*13+time*.65+offset)*.17;
            return h/2+(wave+detail)*amplitude*envelope*.88*bounce;
          };
          ['#F5AD69','#65A84F'].forEach((color,index)=>{
            const paint=ctx!.createLinearGradient(0,0,w,0);
            paint.addColorStop(0,color+'00');paint.addColorStop(.18,color);
            paint.addColorStop(.82,color);paint.addColorStop(1,color+'00');
            for(let strand=4;strand>=0;strand--){
              const offset=index*Math.PI*.85+strand*.11;
              ctx!.beginPath();
              for(let i=0;i<=220;i++){
                const x=i/220,y=listeningSample(x,offset);
                if(i===0)ctx!.moveTo(x*w,y);else ctx!.lineTo(x*w,y);
              }
              ctx!.strokeStyle=paint;ctx!.globalAlpha=strand===0?.95:.24;
              ctx!.lineWidth=strand===0?2.3:1.1;ctx!.lineCap='round';
              ctx!.shadowBlur=0;ctx!.stroke();
            }
          });
        }else{
        for(let ribbon=2;ribbon>=0;ribbon--){
          const offset=ribbon*.28;
          ctx!.beginPath();
          for(let i=0;i<=180;i++){const x=i/180,y=sample(x,offset);if(i===0)ctx!.moveTo(x*w,y);else ctx!.lineTo(x*w,y)}
          for(let i=180;i>=0;i--){const x=i/180;ctx!.lineTo(x*w,sample(x,offset+.38))}
          ctx!.closePath();ctx!.fillStyle=gradient;ctx!.globalAlpha=ribbon===0?.15:.055;ctx!.fill();
        }
        for(let contour=0;contour<3;contour++){
          ctx!.beginPath();
          for(let i=0;i<=180;i++){const x=i/180,y=sample(x,contour*.32);if(i===0)ctx!.moveTo(x*w,y);else ctx!.lineTo(x*w,y)}
          ctx!.strokeStyle=gradient;ctx!.globalAlpha=contour===0?.95:contour===1?.4:.16;
          ctx!.lineWidth=contour===0?2.2:1.2;ctx!.lineCap='round';
          ctx!.shadowBlur=contour===0?5:0;ctx!.shadowColor=colors[1];ctx!.stroke();
        }
        }
        ctx!.shadowBlur=0;ctx!.globalAlpha=1;
      }
      if(!still())frame=requestAnimationFrame(render);
    }
    const wake=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(render)};
    const resize=new ResizeObserver(()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{
      w=canvas.clientWidth;h=canvas.clientHeight;const dpr=Math.min(devicePixelRatio||1,1.5);
      canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);wake();
    })});resize.observe(canvas);
    const observer=new IntersectionObserver(([e])=>{visible=e.isIntersecting;wake()});observer.observe(canvas);
    const move=(e:PointerEvent)=>{if(still())return;const b=canvas.getBoundingClientRect();pointer=(e.clientX-b.left)/b.width;hoverTarget=1};
    const leave=()=>{hoverTarget=0};const tap=()=>{if(!still())kick=1};
    canvas.addEventListener('pointermove',move);canvas.addEventListener('pointerleave',leave);canvas.addEventListener('pointerdown',tap);
    canvas.addEventListener('wave:update',wake);window.addEventListener('razorsharp:motion',wake);document.addEventListener('visibilitychange',wake);reduced.addEventListener('change',wake);wake();
    return()=>{canvas.removeEventListener('pointermove',move);canvas.removeEventListener('pointerleave',leave);canvas.removeEventListener('pointerdown',tap);cancelAnimationFrame(frame);cancelAnimationFrame(resizeFrame);resize.disconnect();observer.disconnect();canvas.removeEventListener('wave:update',wake);window.removeEventListener('razorsharp:motion',wake);document.removeEventListener('visibilitychange',wake);reduced.removeEventListener('change',wake)};
  },[]);
  return <canvas className={`voice-ribbon ${className}`} ref={canvasRef} aria-hidden="true"/>;
}
