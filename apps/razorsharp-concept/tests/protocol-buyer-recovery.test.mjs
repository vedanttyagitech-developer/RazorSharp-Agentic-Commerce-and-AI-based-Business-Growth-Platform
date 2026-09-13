import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness(view, allowed=true) {
  const calls=[];
  class CommerceError extends Error {constructor(status,title){super(title);this.status=status;this.title=title;}}
  const load=(file,imports)=>{
    const exports={};
    const code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
    vm.runInNewContext(code,{exports,require:name=>imports[name],crypto:{randomUUID:()=> 'cancel-key'},sessionStorage:{getItem:()=>null,removeItem:()=>{}}});return exports;
  };
  const recovery=load('../lib/checkout-recovery.ts',{'./commerce':{}});
  const mod=load('../lib/protocol-buyer-recovery.ts',{'./checkout-recovery':recovery,'./commerce':{CommerceError,commerce:{checkout:{read:async()=>view,cancel:async()=>{calls.push('cancel');return {allowed};}}}}});
  return {...mod,CommerceError,calls};
}
const card={checkout_id:'old',quote:{lines:[{sku:'milk',quantity:2}]}};
test('only definitive basket refusals unlock editing; ambiguous and idempotency failures retain the draft',()=>{
  const h=harness({});
  for(const [status,title] of [[409,'Cart cannot be priced'],[404,'Product not found'],[422,'Request validation failed']])assert.equal(h.basketWasRejected(new h.CommerceError(status,title)),true);
  for(const [status,title] of [[0,'Network'],[503,'Unavailable'],[422,'Idempotency conflict'],[409,'Checkout read failed'],[429,'Rate limit']])assert.equal(h.basketWasRejected(new h.CommerceError(status,title)),false);
});
test('expired checkout restores items without cancelling an already-spent checkout',async()=>{
  const h=harness({state:'EXPIRED',order_id:null,attempt:null,cancellable:false});
  assert.equal(JSON.stringify(await h.retireForFreshReview(card)),JSON.stringify([{sku:'milk',quantity:2}]));assert.deepEqual(h.calls,[]);
});
test('fresh review cancels live checkout before restoring basket; denied cancellation blocks it',async()=>{
  for(const allowed of [true,false]){
    const h=harness({state:'APPROVAL_REQUIRED',order_id:null,attempt:null,cancellable:true},allowed);
    if(allowed)await h.retireForFreshReview(card);else await assert.rejects(h.retireForFreshReview(card));
    assert.deepEqual(h.calls,['cancel']);
  }
});
test('pending, unknown and confirmed payments cannot start a replacement purchase',async()=>{
  for(const state of ['PAYMENT_PENDING','PAYMENT_UNKNOWN','CONFIRMED']){
    const h=harness({state,order_id:state==='CONFIRMED'?'order':null,attempt:{state:'PENDING'},cancellable:false});
    await assert.rejects(h.retireForFreshReview(card));assert.deepEqual(h.calls,[]);
  }
});
