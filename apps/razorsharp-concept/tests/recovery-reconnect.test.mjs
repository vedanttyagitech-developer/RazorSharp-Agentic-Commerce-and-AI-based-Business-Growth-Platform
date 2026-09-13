import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness({initial, methodRead, orderRead}) {
 const timers=new Map(),effects=[],states=[],cleanups=[];let sequence=0;
 const react={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:value=>{const i=states.push(value)-1;return[value,next=>{states[i]=typeof next==='function'?next(states[i]):next}]},useRef:current=>({current}),useEffect:fn=>effects.push(fn)};
 class CommerceError extends Error {constructor(status){super('unavailable');this.status=status}}
 const commerce={checkout:{read:async()=>initial},payments:{reconcile:async()=>{throw Error('Unexpected mutation')}},orders:{read:orderRead}};
 const globals={AbortController,crypto,console,setTimeout:fn=>{timers.set(++sequence,fn);return sequence},clearTimeout:id=>timers.delete(id)};
 const load=(path,require)=>{const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../'+path,import.meta.url),'utf8'),{fileName:path,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{...globals,exports,require});return exports};
 const retry=load('lib/recovery-read.ts',()=>({}));
 const mod=load('components/checkout-recovery.tsx',name=>{
  if(name==='react')return react;
  if(name==='react/jsx-runtime')return {jsx:()=>null,jsxs:()=>null};
  if(name==='@/lib/recovery-read')return retry;
  if(name==='@/lib/checkout-events')return {watchCheckout:()=>()=>{}};
  if(name==='@/lib/commerce')return {commerce,CommerceError,rawCommerceCall:(...args)=>methodRead(CommerceError,...args)};
  if(name==='@/lib/checkout-recovery')return {recoveryMessage:()=>'',hasNoPaymentAttempt:v=>!v.attempt,canResumeManualCheckout:()=>false,canRefreshCheckout:()=>false};
  return {};
 });
 const confirmed=[];
 mod.CheckoutRecovery({initial,onOrder(){},onConfirmed:o=>confirmed.push(o)});
 for(const effect of effects){const cleanup=effect();if(cleanup)cleanups.push(cleanup)}
 const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
 return {states,confirmed,flush,tick:async()=>{const batch=[...timers.values()];timers.clear();for(const fn of batch)fn();await flush()},stop:()=>cleanups.forEach(fn=>fn()),timers};
}

test('same attempt retries method lookup after disconnect and becomes manual only on 404',async()=>{
 let reads=0;const h=harness({initial:{checkout_id:'checkout',state:'AWAITING_PAYMENT',attempt:{attempt_id:'same'}},methodRead:async(ErrorType,path,{signal})=>{assert.equal(path,'reserve/payments/same');assert.equal(signal.aborted,false);if(++reads===1)throw new ErrorType(503);throw new ErrorType(404)}});
 await h.flush();assert.equal(h.states[3],null);assert.match(h.states[4],/Retrying/);
 await h.tick();assert.equal(reads,2);assert.equal(h.states[3],'manual');assert.equal(h.states[4],'');
 await h.tick();assert.equal(reads,2,'successful method lookup stops retrying');h.stop();
});

test('same confirmed order retries details and hands off exactly once',async()=>{
 let reads=0;const order={order_id:'paid-order'};
 const h=harness({initial:{checkout_id:'checkout',state:'PAID',order_id:'paid-order'},orderRead:async(id,{aborted})=>{assert.equal(id,'paid-order');assert.equal(aborted,false);if(++reads===1)throw Error('offline');return order}});
 await h.flush();assert.equal(h.confirmed.length,0);assert.match(h.states[5],/order is confirmed/);
 await h.tick();assert.deepEqual(h.confirmed,[order]);assert.equal(h.states[5],'');
 await h.tick();assert.equal(reads,2);assert.equal(h.confirmed.length,1);h.stop();
});

test('cleanup aborts an outstanding read and suppresses its late confirmation',async()=>{
 let finish,signal;const h=harness({initial:{checkout_id:'checkout',state:'PAID',order_id:'old-order'},orderRead:(_id,s)=>{signal=s;return new Promise(resolve=>{finish=resolve})}});
 await h.flush();h.stop();assert.equal(signal.aborted,true);finish({order_id:'old-order'});await h.flush();assert.equal(h.confirmed.length,0);assert.equal(h.timers.size,0);
});

test('cleanup removes pending retry so a closed recovery cannot restart reads',async()=>{
 let reads=0;const h=harness({initial:{checkout_id:'checkout',state:'PAID',order_id:'order'},orderRead:async()=>{reads++;throw Error('offline')}});
 await h.flush();h.stop();await h.tick();assert.equal(reads,1);assert.equal(h.confirmed.length,0);
});
