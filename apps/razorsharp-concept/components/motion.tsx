'use client';

import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { Pause, Play, Sparkles } from 'lucide-react';

const motionEvent = 'razorsharp:motion';

export function MotionToggle() {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    const sync = () => setPaused(document.documentElement.dataset.motion === 'paused');
    sync();
    window.addEventListener(motionEvent, sync);
    return () => window.removeEventListener(motionEvent, sync);
  }, []);
  return <button className="motion-toggle" aria-label={paused ? 'Resume decorative animations' : 'Pause decorative animations'} aria-pressed={paused} title={paused ? 'Resume motion' : 'Pause motion'} onClick={() => {
    document.documentElement.dataset.motion = paused ? 'running' : 'paused';
    window.dispatchEvent(new Event(motionEvent));
  }}>{paused ? <Play size={15}/> : <Pause size={15}/>}</button>;
}

/** Direct style updates keep pointer motion outside React's render loop. */
export function useSurfaceTilt(reveal=false) {
  const surface = useRef<HTMLElement | null>(null);
 useEffect(()=>{const node=surface.current;if(!node||!reveal)return;node.dataset.reveal='pending';const observer=new IntersectionObserver(([entry])=>{if(entry.isIntersecting){node.dataset.reveal='visible';observer.disconnect()}},{threshold:.12});observer.observe(node);return()=>observer.disconnect()},[reveal]);
  const frame = useRef(0);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);
  const reset = () => {
    cancelAnimationFrame(frame.current);
    const style = surface.current?.style;
    style?.setProperty('--tilt-x', '0deg');
    style?.setProperty('--tilt-y', '0deg');
    style?.setProperty('--light-x', '50%');
    style?.setProperty('--light-y', '35%');
  };
  const onPointerMove = (e: PointerEvent<HTMLElement>) => {
    if (e.pointerType !== 'mouse' || window.matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.motion === 'paused') return;
    const bounds = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - bounds.left) / bounds.width;
    const y = (e.clientY - bounds.top) / bounds.height;
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      const style = surface.current?.style;
      style?.setProperty('--tilt-x', `${(0.5-y)*9}deg`);
      style?.setProperty('--tilt-y', `${(x-0.5)*12}deg`);
      style?.setProperty('--light-x', `${x*100}%`);
      style?.setProperty('--light-y', `${y*100}%`);
    });
  };
  return { ref: surface, onPointerMove, onPointerLeave: reset, onPointerCancel: reset };
}

const vertex = `
attribute vec3 aPosition;
attribute vec3 aNormal;
uniform float uTime;
uniform vec2 uPointer;
uniform float uAspect;
varying vec3 vNormal;
varying vec3 vPosition;
mat3 rx(float a){float c=cos(a),s=sin(a);return mat3(1.,0.,0.,0.,c,s,0.,-s,c);}
mat3 ry(float a){float c=cos(a),s=sin(a);return mat3(c,0.,-s,0.,1.,0.,s,0.,c);}
mat3 rz(float a){float c=cos(a),s=sin(a);return mat3(c,s,0.,-s,c,0.,0.,0.,1.);}
void main(){
  mat3 rotation=rz(-.38+sin(uTime*.3)*.09)*ry(.5+sin(uTime*.26)*.32+uPointer.x*.22)*rx(-.32+uPointer.y*.15);
  vec3 p=rotation*aPosition;
  p.y+=sin(uTime*.75)*.045;
  vPosition=p;
  vNormal=rotation*aNormal;
  float depth=4.8-p.z;
  gl_Position=vec4(p.x*2.75/uAspect,p.y*2.75,-p.z,depth);
}`;

const fragment = `
precision mediump float;
uniform vec3 uColor;
varying vec3 vNormal;
varying vec3 vPosition;
void main(){
  vec3 n=normalize(vNormal);
  vec3 eye=normalize(vec3(0.,0.,4.8)-vPosition);
  vec3 key=normalize(vec3(-2.5,4.,5.));
  vec3 fill=normalize(vec3(3.,-1.,2.));
  float diffuse=max(dot(n,key),0.);
  float rim=pow(1.-max(dot(n,eye),0.),2.5);
  float spec=pow(max(dot(n,normalize(key+eye)),0.),75.);
  float softSpec=pow(max(dot(n,normalize(fill+eye)),0.),22.);
  vec3 reflection=reflect(-eye,n);
  float strip=pow(max(0.,1.-abs(reflection.y-.48)*2.8),12.);
  vec3 color=uColor*(.28+diffuse*.76)+vec3(.55,.67,1.)*rim*.55;
  color+=vec3(.95,.98,1.)*(spec*.95+softSpec*.28+strip*.24);
  color+=vec3(.08,.13,.28)*max(dot(n,fill),0.);
  gl_FragColor=vec4(pow(color,vec3(.86)),1.);
}`;

function ringMesh(second: boolean) {
  const vertices: number[] = [];
  const indices: number[] = [];
  const segments = 96, sides = 24;
  for (let i=0;i<=segments;i++) {
    const u=i/segments*Math.PI*2;
    for (let j=0;j<=sides;j++) {
      const v=j/sides*Math.PI*2;
      const r=.68+.195*Math.cos(v);
      const p=[r*Math.cos(u),r*Math.sin(u),.195*Math.sin(v)];
      const n=[Math.cos(v)*Math.cos(u),Math.cos(v)*Math.sin(u),Math.sin(v)];
      if (second) vertices.push(p[2]+.38,p[1],-p[0],n[2],n[1],-n[0]);
      else vertices.push(p[0]-.38,p[1],p[2],...n);
      if (i<segments && j<sides) {
        const a=i*(sides+1)+j,b=a+sides+1;
        indices.push(a,b,a+1,b,b+1,a+1);
      }
    }
  }
  return {vertices:new Float32Array(vertices),indices:new Uint16Array(indices)};
}

/** Two linked, lit 3D surfaces. No imagery, network requests, or render library. */
export function IntelligenceSculpture({className=''}:{className?:string}) {
  const canvasRef=useRef<HTMLCanvasElement>(null);
  const hostRef=useRef<HTMLDivElement>(null);
  useEffect(() => {
    const canvas=canvasRef.current,host=hostRef.current;
    if (!canvas || !host) return;
    const gl=canvas.getContext('webgl',{alpha:true,antialias:true,powerPreference:'low-power'});
    if (!gl) return;
    const program=gl.createProgram();
    if (!program) return;
    const shaders: WebGLShader[]=[];
    for (const [type,source] of [[gl.VERTEX_SHADER,vertex],[gl.FRAGMENT_SHADER,fragment]] as const) {
      const shader=gl.createShader(type);
      if (!shader) { gl.deleteProgram(program); return; }
      gl.shaderSource(shader,source);gl.compileShader(shader);
      if (!gl.getShaderParameter(shader,gl.COMPILE_STATUS)) {
        gl.deleteShader(shader);shaders.forEach(s=>gl.deleteShader(s));gl.deleteProgram(program);return;
      }
      shaders.push(shader);gl.attachShader(program,shader);
    }
    gl.linkProgram(program);
    shaders.forEach(s=>gl.deleteShader(s));
    if (!gl.getProgramParameter(program,gl.LINK_STATUS)) { gl.deleteProgram(program);return; }
    const activateProgram=gl.useProgram.bind(gl);activateProgram(program);gl.enable(gl.DEPTH_TEST);
    const position=gl.getAttribLocation(program,'aPosition');
    const normal=gl.getAttribLocation(program,'aNormal');
    gl.enableVertexAttribArray(position);gl.enableVertexAttribArray(normal);
    const time=gl.getUniformLocation(program,'uTime'),pointer=gl.getUniformLocation(program,'uPointer'),aspect=gl.getUniformLocation(program,'uAspect'),color=gl.getUniformLocation(program,'uColor');
    const meshes=[false,true].map(second=>{
      const data=ringMesh(second),buffer=gl.createBuffer(),index=gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,data.vertices,gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,index);gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,data.indices,gl.STATIC_DRAW);
      return {buffer,index,count:data.indices.length};
    });
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame=0,last=0,elapsed=0,visible=true,typing=false,lost=false;
    let targetX=0,targetY=0,x=0,y=0;
    const isStill=()=>reduced.matches || document.documentElement.dataset.motion==='paused' || typing;
    function draw(now:number) {
      frame=0;
      if (lost || !visible || document.hidden) return;
      if (now-last>=32 || isStill()) {
        if (!isStill()) elapsed+=Math.min((now-last)/1000,.05);
        last=now;
        x+=(targetX-x)*.075;y+=(targetY-y)*.075;
        gl!.viewport(0,0,canvas!.width,canvas!.height);gl!.clearColor(0,0,0,0);gl!.clear(gl!.COLOR_BUFFER_BIT|gl!.DEPTH_BUFFER_BIT);
        gl!.uniform1f(time,elapsed);gl!.uniform2f(pointer,x,y);gl!.uniform1f(aspect,canvas!.width/canvas!.height);
        meshes.forEach((m,i)=>{
          gl!.bindBuffer(gl!.ARRAY_BUFFER,m.buffer);gl!.vertexAttribPointer(position,3,gl!.FLOAT,false,24,0);gl!.vertexAttribPointer(normal,3,gl!.FLOAT,false,24,12);
          gl!.bindBuffer(gl!.ELEMENT_ARRAY_BUFFER,m.index);
          gl!.uniform3f(color,...(i?[.64,.74,.93]:[.12,.24,.94]) as [number,number,number]);
          gl!.drawElements(gl!.TRIANGLES,m.count,gl!.UNSIGNED_SHORT,0);
        });
        host!.dataset.rendered='true';
      }
      if (!isStill()) frame=requestAnimationFrame(draw);
    }
    const wake=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(draw);};
    let resizeFrame=0;
    const resize=new ResizeObserver(()=>{
      cancelAnimationFrame(resizeFrame);
      resizeFrame=requestAnimationFrame(()=>{
        const b=host.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,1.75);
        const width=Math.max(1,Math.round(b.width*dpr)),height=Math.max(1,Math.round(b.height*dpr));
        if(canvas.width!==width)canvas.width=width;
        if(canvas.height!==height)canvas.height=height;
        wake();
      });
    });
    resize.observe(host);
    const observer=new IntersectionObserver(([entry])=>{visible=entry.isIntersecting;wake();});observer.observe(host);
    const scope=host.closest('.shop-intro,.merchant-copilot,.merchant-art') || host;
    const move=(e:Event)=>{if(isStill())return;const p=e as globalThis.PointerEvent;if(p.pointerType!=='mouse')return;const b=scope.getBoundingClientRect();targetX=(p.clientX-b.left)/b.width-.5;targetY=(p.clientY-b.top)/b.height-.5;};
    const leave=()=>{targetX=0;targetY=0;};
    const focus=()=>{typing=!!scope.querySelector('textarea:focus,input:focus');wake();};
    const blur=()=>{typing=false;wake();};
    const contextLost=(e:Event)=>{e.preventDefault();lost=true;host.dataset.rendered='false';cancelAnimationFrame(frame);};
    scope.addEventListener('pointermove',move);scope.addEventListener('pointerleave',leave);scope.addEventListener('focusin',focus);scope.addEventListener('focusout',blur);
    canvas.addEventListener('webglcontextlost',contextLost);
    document.addEventListener('visibilitychange',wake);window.addEventListener(motionEvent,wake);reduced.addEventListener('change',wake);
    wake();
    return ()=>{
      cancelAnimationFrame(frame);cancelAnimationFrame(resizeFrame);resize.disconnect();observer.disconnect();
      scope.removeEventListener('pointermove',move);scope.removeEventListener('pointerleave',leave);scope.removeEventListener('focusin',focus);scope.removeEventListener('focusout',blur);
      canvas.removeEventListener('webglcontextlost',contextLost);document.removeEventListener('visibilitychange',wake);window.removeEventListener(motionEvent,wake);reduced.removeEventListener('change',wake);
      meshes.forEach(m=>{gl.deleteBuffer(m.buffer);gl.deleteBuffer(m.index);});gl.deleteProgram(program);
    };
  },[]);
  return <div className={`intelligence-sculpture ${className}`} ref={hostRef} aria-hidden="true"><div className="sculpture-aura"/><div className="sculpture-fallback"><Sparkles size={35}/></div><canvas ref={canvasRef}/><div className="sculpture-shadow"/></div>;
}
