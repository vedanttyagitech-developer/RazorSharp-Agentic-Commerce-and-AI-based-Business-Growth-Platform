import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
test('Reconnect is bounded, restores only guidance, and Finish cancels retries',async()=>{
 const clients=[],timers=new Map();let seq=0;
 class Client {
  constructor(events){this.events=events;this.guidance=[];clients.push(this)}
  async open(){this.events.onReady()}
  async close(){}
  checkoutGuidance(...args){this.guidance.push(args)}
  text(){assert.fail('Must not replay commands')}
 }
 const react={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,createContext:()=>({Provider:'provider'}),useCallback:f=>f,useEffect:()=>{},useMemo:f=>f(),useRef:v=>({current:v}),useState:v=>[v,()=>{}]};
 const exports={};
 const code=ts.transpileModule(readFileSync(new URL('../components/voice-session.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{requestAnimationFrame:fn=>{fn();return 0},cancelAnimationFrame:()=>{},exports,console,DOMException,setTimeout:f=>{timers.set(++seq,f);return seq},clearTimeout:id=>timers.delete(id),require:n=>n==='react'?react:n==='react/jsx-runtime'?{jsx:(_,props)=>props}:n.includes('voice/client')?{VoiceClient:Client}:n.includes('voice/conversation')?{conversationPhase:()=> 'idle'}:{}});
 const api=exports.VoiceSessionProvider({children:null}).value;
 await api.connect();api.checkoutGuidance('checkout-1','review',2);
 async function tick(){for(let i=0;i<8;i++)await Promise.resolve()}
 for(let i=0;i<3;i++){
  clients.at(-1).events.onClosed('network lost',true);assert.equal(timers.size,1);
  const [id,fn]=timers.entries().next().value;timers.delete(id);fn();await tick();
  assert.deepEqual(Array.from(clients.at(-1).guidance[0]),['checkout-1','review',2]);
 }
 clients.at(-1).events.onClosed('network lost',true);assert.equal(timers.size,0);assert.equal(clients.length,4);
 api.startListening();await tick();
 clients.at(-1).events.onClosed('network lost',true);assert.equal(timers.size,1);
 api.finishListening();assert.equal(timers.size,0);
});
