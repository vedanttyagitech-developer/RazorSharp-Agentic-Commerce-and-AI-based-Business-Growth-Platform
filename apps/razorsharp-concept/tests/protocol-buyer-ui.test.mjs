import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness(rejected, view = null, catalogue = null) {
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
  vm.runInNewContext(code,{exports,URLSearchParams,queueMicrotask,Intl,crypto:{randomUUID:()=>`key-${requests.length}`},URL,window:{location:{search:view?'?buyerProtocol=ACP&checkout=old':'',href:'http://localhost/platform/?buyerProtocol=ACP&checkout=old'},history:{replaceState(){}}},sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},setInterval:()=>1,clearInterval:()=>{},require(name){
    if(name==='react')return react;if(name==='react/jsx-runtime')return {jsx,jsxs:jsx};
    if(name==='@/lib/checkout-recovery')return {canRefreshCheckout:v=>!v.order_id&&!v.attempt&&['EXPIRED','CANCELLED'].includes(v.state),isSpentCheckout:v=>!v.order_id&&!v.attempt&&['EXPIRED','CANCELLED'].includes(v.state)};
    if(name==='@/lib/protocol-buyer-recovery')return {basketWasRejected:()=>rejected};
    if(name==='@/lib/commerce')return {commerce:{checkout:{read:async()=>view},orders:{read:async()=>({id:view.order_id})},catalogue:catalogue ?? {list:async()=>({products:[{sku:'milk',display_name:'Milk',unit_price_minor:100,currency:'INR',stock_units:5,is_available:true,is_listed:true}]})}},rawCommerceCall:async(path,options)=>{requests.push(options);if(view)return {checkout:view,protocol_response:{}};throw Error('Checkout refused');}};
    return new Proxy({},{get:(_,key)=>String(key)});
  }});
  function nodes(n,out=[]){if(!n||typeof n!=='object')return out;if(Array.isArray(n)){n.forEach(x=>nodes(x,out));return out;}out.push(n);nodes(n.props?.children,out);return out;}
  function draw(){slot=effectSlot=callbackSlot=0;tree=exports.ProtocolBuyerJourney({protocol:'ACP'});}
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
