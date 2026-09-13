import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

class AuditError extends Error { constructor(){super('Checkout changed');this.status=409;this.title='Checkout changed';} }

function harness(rejected, view = null, catalogue = null, protocol = 'ACP', updateCall = null, cancelCall = null, readCall = null) {
  const state=[], effects=[], callbacks=[], saved=new Map(), requests=[];let slot=0, effectSlot=0, callbackSlot=0,tree;
  const jsx=(type,props)=>({type,props:props||{}});
  const react={
    useState(initial){const i=slot++;if(!(i in state))state[i]=typeof initial==='function'?initial():initial;return [state[i],v=>{state[i]=typeof v==='function'?v(state[i]):v;}];},
    useRef(initial){return react.useState({current:initial})[0];},
    useCallback(fn,deps){const i=callbackSlot++;if(!callbacks[i]||deps.some((d,j)=>d!==callbacks[i].deps[j]))callbacks[i]={fn,deps};return callbacks[i].fn;},
    useEffect(fn,deps){const i=effectSlot++;if(!effects[i]||deps.some((d,j)=>d!==effects[i].deps[j])){effects[i]?.cleanup?.();effects[i]={deps,cleanup:fn()};}},
  };
  const exports={};
  const source=readFileSync(new URL('../components/protocol-buyer-journey.tsx',import.meta.url),'utf8');
  const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(code,{exports,URLSearchParams,queueMicrotask,Intl,crypto:{randomUUID:()=>`key-${requests.length}`},URL,window:{location:{search:view?`?buyerProtocol=${protocol}&checkout=old`:'',href:'http://localhost/platform/?buyerProtocol=ACP&checkout=old'},history:{replaceState(){}}},sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},setInterval:()=>1,clearInterval:()=>{},require(name){
    if(name==='react')return react;if(name==='react/jsx-runtime')return {jsx,jsxs:jsx};
    if(name==='@/lib/checkout-recovery')return {canRefreshCheckout:v=>!v.order_id&&!v.attempt&&(v.cancellable||['EXPIRED','CANCELLED'].includes(v.state)),clearCancelledCheckoutRecovery:()=>{},isSpentCheckout:v=>!v.order_id&&!v.attempt&&['EXPIRED','CANCELLED'].includes(v.state)};
    if(name==='@/lib/protocol-buyer-recovery')return {basketWasRejected:()=>rejected};
    if(name==='@/lib/commerce')return {CommerceError:AuditError,commerce:{checkout:{cancel:async()=>{if(cancelCall)return cancelCall();view.state='CANCELLED';view.cancellable=false;return {allowed:true};},read:async()=>view},orders:{read:async()=>({id:view.order_id})},catalogue:catalogue ?? {list:async()=>({products:[{sku:'milk',display_name:'Milk',unit_price_minor:100,currency:'INR',stock_units:5,is_available:true,is_listed:true}]})}},rawCommerceCall:async(path,options)=>{requests.push(options);if(options?.method==='PUT' && updateCall)return updateCall(path,options);if(readCall)return readCall();if(view)return {checkout:view,protocol_response:{}};throw Error('Checkout refused');}};
    return new Proxy({},{get:(_,key)=>String(key)});
  }});
  function nodes(n,out=[]){if(!n||typeof n!=='object')return out;if(Array.isArray(n)){n.forEach(x=>nodes(x,out));return out;}out.push(n);nodes(n.props?.children,out);return out;}
  function draw(){slot=effectSlot=callbackSlot=0;tree=exports.ProtocolBuyerJourney({protocol});}
  async function flush(){await new Promise(r=>setImmediate(r));draw();await new Promise(r=>setImmediate(r));draw();}
  return {draw,flush,requests,saved,find:predicate=>nodes(tree).find(predicate)};
}
for(const rejected of [true,false])test(`checkout error ${rejected?'unlocks rejected basket':'preserves ambiguous request'}`,async()=>{
 const h=harness(rejected);h.draw();await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Discover products').props.onClick();await h.flush();
 h.find(n=>n.type==='input'&&n.props.type==='number').props.onChange({target:{value:'2'}});await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Create ACP checkout & review').props.onClick();await h.flush();
 assert.equal(h.find(n=>n.type==='input'&&n.props.type==='number').props.disabled,!rejected);
 assert.equal(h.saved.has('protocol-buyer:ACP'),!rejected);
 if(!rejected){h.find(n=>n.type==='button'&&n.props.children==='Recover checkout request').props.onClick();await h.flush();assert.equal(h.requests[0].idempotencyKey,h.requests[1].idempotencyKey);}
});

test('protocol enable label follows backend status on mount and subsequent refresh',async()=>{
 const state=[],effects=[];let slot=0,es=0,tree,poll,enabled=true;
 const jsx=(type,props)=>({type,props:props||{}});
 const api=async()=>({enabled:{ACP:enabled,UCP:false,MCP:false}});
 const react={useState(initial){const i=slot++;if(!(i in state))state[i]=initial;return [state[i],v=>{state[i]=typeof v==='function'?v(state[i]):v;}];},useEffect(fn,deps){const i=es++;if(!effects[i])effects[i]={cleanup:fn(),deps};}};
 const exports={};
 const code=ts.transpileModule(readFileSync(new URL('../components/protocol-lab.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,setInterval:fn=>{poll=fn;return 1;},clearInterval:()=>{},require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx}:{EvidenceDocument:'evidence'}});
 const draw=()=>{slot=es=0;tree=exports.ProtocolLab({api,protocol:'ACP'});};
 draw();await new Promise(r=>setImmediate(r));draw();assert.match(JSON.stringify(tree),/Demo testing enabled/);
 enabled=false;poll();await new Promise(r=>setImmediate(r));draw();assert.match(JSON.stringify(tree),/Enable ACP demo testing/);assert.doesNotMatch(JSON.stringify(tree),/Demo testing enabled/);
});

for (const terminal of ['EXPIRED','CANCELLED']) test(`deep link ${terminal} without a draft offers a safe fresh basket`,async()=>{
 const h=harness(false,{state:terminal,order_id:null,attempt:null});h.draw();await h.flush();
 const button=h.find(n=>n.type==='button'&&n.props.children==='Refresh stock and review again');assert.ok(button);
 button.props.onClick();await h.flush();
 assert.ok(h.find(n=>n.type==='button'&&n.props.children==='Discover products'));
});
for (const state of ['UNKNOWN','SUBMITTED','ESCALATED','CAPTURED']) test(`resumed ${state} payment cannot reset journey`,async()=>{
 const h=harness(false,{state:'PAYMENT_UNKNOWN',order_id:null,attempt:{state}});h.draw();await h.flush();
 assert.equal(h.find(n=>n.type==='button'&&['Start another purchase','Refresh stock and review again','Discover products'].includes(n.props.children)),undefined);
});
test('confirmed order can start a separate purchase',async()=>{
 const h=harness(false,{state:'COMPLETED',order_id:'order-1',attempt:{state:'CAPTURED'}});h.draw();await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Start another purchase').props.onClick();await h.flush();
 assert.ok(h.find(n=>n.type==='button'&&n.props.children==='Discover products'));assert.equal(h.saved.size,0);
});

const milk = {sku:'milk',display_name:'Milk',unit_price_minor:100,currency:'INR',stock_units:5,is_available:true,is_listed:true};
test('selected unavailable item remains removable after catalogue refresh',async()=>{
 let count=0;
 const h=harness(true,null,{list:async()=>({products:[++count===1?milk:{...milk,stock_units:0,is_available:false,is_listed:false}]})});
 h.draw();await h.flush();
 const click=async text=>{h.find(n=>n.type==='button'&&n.props.children===text).props.onClick();await h.flush();};
 await click('Discover products');
 h.find(n=>n.type==='input'&&n.props.type==='number').props.onChange({target:{value:'2'}});await h.flush();
 await click('Discover products');
 const remove=h.find(n=>n.type==='button'&&JSON.stringify(n.props.children).includes('Remove '));
 assert.equal(remove.props.disabled,false);remove.props.onClick();await h.flush();
 assert.equal(h.find(n=>n.type==='input'&&n.props['aria-label']==='Basket quantity of Milk'),undefined);
 assert.equal(h.find(n=>n.type==='button'&&n.props.children==='Create ACP checkout & review').props.disabled,true);
});

test('catalogue cursor is consumed and search cannot hide the selected basket editor',async()=>{
 const calls=[];
 const h=harness(true,null,{list:async opts=>{calls.push(opts);return {products:opts.cursor?[{...milk,sku:'bread',display_name:'Bread'}]:[milk],next_cursor:opts.cursor?null:'milk'};},search:async()=>({hits:[]})});
 h.draw();await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Discover products').props.onClick();await h.flush();
 h.find(n=>n.type==='input'&&n.props.type==='number').props.onChange({target:{value:'2'}});await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Load more products').props.onClick();await h.flush();
 assert.equal(calls[1].cursor,'milk');
 assert.ok(h.find(n=>n.type==='input'&&n.props['aria-label']==='Quantity of Bread'));
 h.find(n=>n.type==='input'&&n.props.placeholder==='Search the real catalogue').props.onChange({target:{value:'unmatched'}});await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Discover products').props.onClick();await h.flush();
 assert.equal(h.find(n=>n.type==='input'&&n.props['aria-label']==='Basket quantity of Milk').props.value,2);
 assert.ok(h.find(n=>n.type==='button'&&n.props.children==='Browse full catalogue'));
});

test('fresh review traverses every catalogue page',async()=>{
 const cursors=[];
 const h=harness(false,{state:'EXPIRED',order_id:null,attempt:null},{list:async opts=>{cursors.push(opts.cursor);return {products:[{...milk,sku:String(cursors.length),display_name:`Page ${cursors.length}`}],next_cursor:cursors.length<3?String(cursors.length):null};}});
 h.draw();await h.flush();
 h.find(n=>n.type==='button'&&n.props.children==='Refresh stock and review again').props.onClick();await h.flush();
 assert.deepEqual(cursors,[undefined,'1','2']);
 assert.ok(h.find(n=>n.type==='input'&&n.props['aria-label']==='Quantity of Page 3'));
});


for (const protocol of ['ACP', 'UCP']) test(`${protocol} edits checkout and preserves unknown update key`, async()=>{
 const card={checkout_id:'old',version:1,content_hash:'hash-1',amount_minor:100,currency:'INR',quote:{lines:[{sku:'milk',name:'Milk',quantity:1,subtotal_minor:100}]}};
 const view={state:'APPROVAL_REQUIRED',order_id:null,attempt:null,cancellable:true,approval_card:card};
 const calls=[];
 const h=harness(false,view,null,protocol,async(path,opts)=>{calls.push({path,opts});if(calls.length===1)throw Error('Network timeout');view.approval_card={...card,version:2,content_hash:'hash-2'};return {card:view.approval_card};});
 h.draw();await h.flush();
 const click=async text=>{const n=h.find(n=>n.type==='button'&&n.props.children===text);assert.ok(n,text);n.props.onClick();await h.flush();};
 await click('Edit checkout basket');
 h.find(n=>n.type==='input'&&n.props['aria-label']==='Basket quantity of milk').props.onChange({target:{value:'2'}});await h.flush();
 await click('Review updated basket');
 assert.ok(h.find(n=>n.type==='button'&&n.props.children==='Recover checkout update'));
 assert.equal(h.find(n=>n.type==='button'&&n.props.children==='Continue to buyer approval & Razorpay'),undefined);
 await click('Recover checkout update');
 assert.equal(calls[0].path,`buyer-protocols/${protocol}/checkouts/old`);
 assert.equal(calls[0].opts.idempotencyKey,calls[1].opts.idempotencyKey);
 assert.equal(calls[0].opts.body.items[0].quantity,2);
 assert.equal(calls[0].opts.body.content_hash,'hash-1');
 assert.equal(JSON.parse(h.saved.get(`protocol-buyer:${protocol}`)).card.version,2);
 await click('Cancel checkout');
 assert.equal(view.state,'CANCELLED');
 assert.equal(h.find(n=>n.type==='button'&&n.props.children==='Continue to buyer approval & Razorpay'),undefined);
});

for (const protocol of ['ACP','UCP']) test(`${protocol} stale retry recovery`,async()=>{
const card={checkout_id:'old',version:1,content_hash:'h1',amount_minor:100,currency:'INR',quote:{lines:[{sku:'milk',name:'Milk',quantity:1,subtotal_minor:100}]}};
const view={state:'APPROVAL_REQUIRED',order_id:null,attempt:null,cancellable:true,approval_card:card};let attempts=0;
const h=harness(false,view,null,protocol,async()=>{if(++attempts===1)throw Error('timeout');view.approval_card={...card,version:2,content_hash:'h2'};throw new AuditError();});
h.draw();await h.flush();
const click=async text=>{const b=h.find(n=>n.type==='button'&&n.props.children===text);assert.ok(b,text);b.props.onClick();await h.flush();};
await click('Edit checkout basket');await click('Review updated basket');await click('Recover checkout update');
const saved=JSON.parse(h.saved.get(`protocol-buyer:${protocol}`));
assert.equal(saved.card.version,2);
assert.ok(h.find(n=>n.props.children==='Continue to buyer approval & Razorpay'));
assert.equal(saved.update,undefined,'Definitive rejection should clear pending update');
});

for (const protocol of ['ACP','UCP']) test(`${protocol} cancellation refusal clears retry state despite changed card`,async()=>{
 const card={checkout_id:'old',version:1,content_hash:'h1',amount_minor:100,currency:'INR',quote:{lines:[{sku:'milk',name:'Milk',quantity:1,subtotal_minor:100}]}};
 const view={state:'APPROVAL_REQUIRED',order_id:null,attempt:null,cancellable:true,approval_card:card};let attempts=0;
 const h=harness(false,view,null,protocol,null,async()=>{if(++attempts===1)throw Error('timeout');view.approval_card={...card,version:2,content_hash:'h2'};return {allowed:false,explanation:'Checkout changed'};});
 h.draw();await h.flush();
 const click=async text=>{h.find(n=>n.props.children===text).props.onClick();await h.flush();};
 await click('Cancel checkout');await click('Retry cancellation request');
 assert.equal(JSON.parse(h.saved.get(`protocol-buyer:${protocol}`)).cancelKey,undefined);
 assert.equal(h.find(n=>n.props.children==='Edit checkout basket').props.disabled,false);
});

for(const protocol of ['ACP','UCP'])test(`${protocol} late status cannot roll back newer checkout`,async()=>{
 const card={checkout_id:'old',version:1,content_hash:'h1',amount_minor:100,currency:'INR',quote:{lines:[{sku:'milk',name:'Milk',quantity:1,subtotal_minor:100}]}};
 const view={state:'APPROVAL_REQUIRED',order_id:null,attempt:null,cancellable:true,approval_card:card};
 let queued=false;const readers=[];
 const h=harness(false,view,null,protocol,null,null,()=>queued?new Promise(resolve=>readers.push(resolve)):Promise.resolve({checkout:view,protocol_response:{}}));
 h.draw();await h.flush();queued=true;
 const button=h.find(n=>n.props.children==='Refresh payment / order status');
 button.props.onClick();button.props.onClick();
 queued=false;view.approval_card={...card,version:2,content_hash:'h2'};
 readers[1]({checkout:{...view},protocol_response:{}});await h.flush();
 readers[0]({checkout:{...view,approval_card:card},protocol_response:{}});await h.flush();
 assert.equal(JSON.parse(h.saved.get(`protocol-buyer:${protocol}`)).card.version,2);
});
