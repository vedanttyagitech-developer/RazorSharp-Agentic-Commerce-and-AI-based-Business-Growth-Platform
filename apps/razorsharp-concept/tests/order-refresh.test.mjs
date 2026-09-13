import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

test('refresh orders reloads selected details and the latest merchant response', async () => {
  const instances = new Map(); let current, tree, details, reads=0, caseReads=0;
  const jsx=(type,props,key)=>({type,props:props||{},key});
  const react={
    useState(initial){const owner=current,i=owner.slot++;if(!(i in owner.state))owner.state[i]=initial;return [owner.state[i],v=>{owner.state[i]=typeof v==='function'?v(owner.state[i]):v;}];},
    useRef(initial){const [ref]=react.useState({current:initial});return ref;},
    useEffect(fn,deps){const owner=current,i=owner.effectSlot++,old=owner.effects[i];if(!old||deps.some((d,j)=>d!==old.deps[j])){old?.cleanup?.();owner.effects[i]={deps,cleanup:fn()};}},
  };
  const row={order_id:'order-1',reference:'RS-test',created_at:'2026-09-12',amount:{display:'10.00'},state:'CONFIRMED'};
  const exports={};
  const code=ts.transpileModule(readFileSync(new URL('../components/live-orders.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(code,{exports,AbortController,queueMicrotask,require(name){
    if(name==='react')return react;
    if(name==='react/jsx-runtime')return {jsx,jsxs:jsx};
    if(name==='@/lib/checkout-events')return {watchCheckout:()=>()=>{}};
    if(name==='@/lib/commerce')return {commerce:{orders:{list:async()=>({orders:[row],next_cursor:null}),read:async()=>{reads++;return {...row,checkout_id:'checkout-1'};}}},rawCommerceCall:async path=>{if(path.endsWith('/support-cases')){caseReads++;return {cases:[{case_id:'case-1',status:'RESOLVED',reason:'damaged',resolution_note:caseReads===1?'First reply':'Updated reply'}]};}return {entries:[],verdict:{checks:[]}};}};
    return new Proxy({},{get:(_,name)=>String(name)});
  }});
  function render(type,props,key){if(!instances.has(key))instances.set(key,{state:[],effects:[]});current=instances.get(key);current.slot=0;current.effectSlot=0;return type(props);}
  function nodes(node,result=[]){if(!node||typeof node!=='object')return result;if(Array.isArray(node)){node.forEach(n=>nodes(n,result));return result;}result.push(node);nodes(node.props?.children,result);return result;}
  function draw(){tree=render(exports.LiveOrders,{support:true},'parent');const child=nodes(tree).find(n=>typeof n.type==='function'&&n.type.name==='OrderDetail');if(child)details=render(child.type,child.props,child.key);}
  async function flush(){await new Promise(r=>setImmediate(r));draw();await new Promise(r=>setImmediate(r));draw();}
  draw();await flush();nodes(tree).find(n=>n.type==='button'&&n.props['aria-pressed']===false).props.onClick();await flush();
  assert.equal(reads,1);assert.equal(caseReads,1);assert.match(JSON.stringify(details),/First reply/);
  nodes(tree).find(n=>n.type==='button'&&n.props.children==='Refresh orders').props.onClick();await flush();
  assert.equal(reads,2);assert.equal(caseReads,2);assert.match(JSON.stringify(details),/Updated reply/);
  for(const instance of instances.values())for(const effect of instance.effects)effect?.cleanup?.();
});
