import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness({rejection=false,expired=false,permission=true,pending=null}={}){
 const states=[],refs=[],effects=[],calls=[];let slot=0,refSlot=0,effectSlot=0,jobs=[];
 let releasePermissions;const permissionRead=new Promise(resolve=>releasePermissions=resolve);
 const authority={authority_id:'authority',epoch:1,status:'ACTIVE',authorization_evidence:{status:'VERIFIED'},available_minor:1000,capacity_minor:1000,per_purchase_limit_minor:500,allowed_skus:null};
 const card={checkout_id:'checkout',version:1,content_hash:'reviewed-hash',amount_minor:100,currency:'INR',quote:{lines:[],items_subtotal_minor:100,items_tax_minor:0,delivery_tax_minor:0,delivery_fee_minor:0,discount_minor:0},...(expired?{reservation:{expires_at:'2020-01-01'}}:{})};
 const react={useState(initial){const i=slot++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return [states[i],value=>states[i]=typeof value==='function'?value(states[i]):value]},useRef(initial){const i=refSlot++;return refs[i]??={current:initial}},useLayoutEffect(fn){fn()},useEffectEvent:fn=>fn,useEffect(fn,deps){const i=effectSlot++;if(!effects[i]||deps.some((v,k)=>!Object.is(v,effects[i].deps[k]))){effects[i]?.cleanup?.();effects[i]={deps};jobs.push(()=>effects[i].cleanup=fn())}}};
 const jsx=(type,props)=>({type,props});const exports={};let saved=pending?JSON.stringify({...pending,card,authority}):null;
 const code=ts.transpileModule(readFileSync(new URL('../components/reserve-checkout.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,crypto,sessionStorage:{getItem:()=>saved,setItem:(_k,v)=>saved=v,removeItem:()=>saved=null},setInterval:()=>1,clearInterval:()=>{},require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx}:name.endsWith('reserve-api')?{permissions:()=>permissionRead,commerce:async(path,method,body,key)=>{calls.push({path,method,body,key});return rejection?{allowed:false,code:'REAPPROVAL_REQUIRED',approval_card:{...card,version:2,content_hash:'changed-hash',amount_minor:80}}:{allowed:true,attempt_id:'same-attempt'}}}:name.endsWith('reserve-recovery')?{reserveRequestSettled:async()=>false}:name.endsWith('payment-animation')?{paymentAnimationState:()=> 'processing'}:name.endsWith('demo')?{money:String}:{}});
 const props={total:100,reviewed:card,commitRequest:1,onConfirmed:()=>{},onBack:()=>{},onGuidance:()=>{},onFreshReview:async()=>{}};
 function draw(){slot=refSlot=effectSlot=0;exports.ReserveCheckout(props);const run=jobs;jobs=[];run.forEach(fn=>fn())}
 async function settle(){for(let i=0;i<4;i++){await new Promise(resolve=>setImmediate(resolve));draw()}}
 draw();return {draw,settle,calls,release:()=>releasePermissions({authorities:permission?[authority]:[]}),props};
}

test('one explicit Reserve click waits for readiness then admits exactly the reviewed bill once',async()=>{
 const h=harness();await h.settle();assert.equal(h.calls.length,0);h.release();await h.settle();
 const pays=h.calls.filter(c=>c.method==='POST');assert.equal(pays.length,1);assert.equal(pays[0].path,'reserve/checkouts/checkout/versions/1/pay');assert.equal(pays[0].body.content_hash,'reviewed-hash');assert.equal(pays[0].body.amount_minor,100);assert.ok(pays[0].key);
 await h.settle();assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});
test('changed lower bill is never auto-paid by the old click',async()=>{
 const h=harness({rejection:true});h.release();await h.settle();await h.settle();assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});
test('expired permission scope and an existing unresolved request never auto-pay',async()=>{
 for(const options of [{expired:true},{permission:false},{pending:{key:'old-key',attemptId:'old-attempt'}}]){const h=harness(options);h.release();await h.settle();assert.equal(h.calls.filter(c=>c.method==='POST').length,0)}
});
