import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

test('provider dismissal closes the parent once and preserves the existing pending payment',async()=>{
 let closed=0,approved=0,pending=null,settled=0;
 const hooks={useState:v=>[v,()=>{}],useRef:v=>({current:v}),useCallback:f=>f,useEffectEvent:f=>f,useEffect:f=>f()};
 const exports={};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/manual-checkout.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,AbortController,requestAnimationFrame:f=>{f();return 1},cancelAnimationFrame:()=>{},require(name){
  if(name==='react')return hooks;
  if(name==='react/jsx-runtime')return {jsx:()=>null,jsxs:()=>null};
  if(name==='@/lib/demo')return {money:String};
  if(name==='@/lib/manual-pay')return {pendingFor:()=>({checkoutId:'existing',keys:{verify:'stable'}}),approve:async()=>{approved++;return {code:'ADMITTED'}},writePending:v=>{pending=v},awaitProviderOrder:async()=>({razorpay_key_id:'rzp_test_demo',razorpay_order_id:'same-order',amount_minor:5750,currency:'INR'}),awaitSettlement:async()=>{settled++;throw Error('Parent must close on dismissal')},clearPending:()=>assert.fail('Do not erase unknown payment'),manualMessage:e=>({title:e.message})};
  if(name==='@/lib/razorpay')return {loadRazorpay:async()=>{},isTestKey:()=>true,openRazorpay:async()=>({kind:'dismissed'}),RazorpayUnavailableError:class extends Error{}};
  if(name==='@/lib/commerce')return {commerce:{},CommerceError:class extends Error{},KernelRefusal:class extends Error{}};
  return {};
 }});
 exports.ManualCheckout({card:{checkout_id:'existing',amount_minor:5750,quote:{}},startRequest:1,onDismiss:()=>closed++,onProviderOpen:()=>{},onVoiceStage:()=>{},onGuidance:()=>{},onConfirmed:()=>assert.fail('Dismissal is not confirmation')});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(closed,1);assert.equal(approved,1);assert.equal(pending.checkoutId,'existing');assert.equal(settled,0);
});
