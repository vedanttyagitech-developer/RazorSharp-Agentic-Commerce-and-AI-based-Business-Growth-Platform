import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

// Render the real assistant with a small hook runner to exercise effect dependencies.
function fixture() {
  const slots=[], pending=[], calls=[];
  let cursor=0;
  const voice={live:true, checkoutGuidance:(...args)=>calls.push(args), speak(){}};
  const effect=(fn,deps)=>{
    const index=cursor++, previous=slots[index];
    if(!previous || !deps || deps.some((v,i)=>!Object.is(v,previous[i])))pending.push(fn);
    slots[index]=deps;
  };
  const react={useEffect:effect,useLayoutEffect:effect,
    useRef:value=>{const index=cursor++;return slots[index]??(slots[index]={current:value})},
    useState:value=>{const index=cursor++;if(!(index in slots))slots[index]=value;return [slots[index],v=>{slots[index]=v}]}};
  const exports={};
  const code=ts.transpileModule(readFileSync(new URL('../components/checkout-payment.tsx',import.meta.url),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},
  }).outputText;
  vm.runInNewContext(code,{exports,require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx(){},jsxs(){}}:name==='./voice-session'?{useVoiceSession:()=>voice}:{}});
  return {calls,render(message,checkoutStage='manual'){
    cursor=0;
    exports.CheckoutAssistant({message,checkoutStage,checkoutId:'checkout',version:1});
    while(pending.length)pending.shift()();
  }};
}
test('Live checkout refreshes verified speech when settlement changes only the message',()=>{
  const f=fixture();
  f.render('Complete payment in Razorpay');
  assert.equal(f.calls.length,1);
  f.render('The backend confirmed this payment did not complete.');
  assert.equal(f.calls.length,2);
  assert.deepEqual(f.calls[1],['checkout','manual',1]);
  f.render('The backend confirmed this payment did not complete.');
  assert.equal(f.calls.length,2,'Unchanged renders must not repeat speech');
});
test('Verification and failure stages request new backend guidance',()=>{
  const f=fixture();
  f.render('Checking payment','manual');
  f.render('Checking payment','verifying');
  f.render('Checking payment','failed');
  assert.deepEqual(f.calls.map(call=>call[1]),['manual','verifying','failed']);
});
