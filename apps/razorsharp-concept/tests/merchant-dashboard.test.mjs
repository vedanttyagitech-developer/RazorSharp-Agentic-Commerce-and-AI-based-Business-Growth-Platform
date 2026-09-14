import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
test('dashboard retains successful records when support is unavailable and opens the exact order',async()=>{
 const states=[];let index=0,initial=true;const exports={};const calls=[];let destination;
 const jsx=(type,props)=>({type:typeof type==='string'?type:'component',props});
 const hooks={useState(value){const slot=index++;if(initial)states[slot]=value;return [states[slot],value=>{states[slot]=typeof value==='function'?value(states[slot]):value}]},useEffect(fn){if(initial)fn()}};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/merchant-dashboard.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,Error,require(name){if(name==='react')return hooks;if(name==='react/jsx-runtime')return {jsx,jsxs:jsx};if(name==='@/lib/catalogue')return {useCatalogue:()=>({products:[],loading:false,error:null})};if(name==='@/lib/merchant-facts')return {formatMinor:(v,c)=>`${c} ${v}`,LOW_STOCK_UNITS:10};if(name==='./live-merchant')return {merchantCall:async path=>{calls.push(path);if(path.startsWith('support'))throw Error('Support offline');if(path.startsWith('orders'))return {orders:[{order_id:'order-exact',reference:'RS-EXACT',created_at:'2026-09-13T00:00:00Z',amount_minor:100,currency:'INR',state:'CONFIRMED'}]};if(path.startsWith('merchant/insights'))return {totals:[{currency:'INR',orders:1,sales_minor:100},{currency:'USD',orders:1,sales_minor:200}],definition:'Recorded orders'};return {actions:[],may_have_more:false}}};return {}}});
 const props={revision:0,onNavigate:(...args)=>destination=args};exports.MerchantDashboard(props);await new Promise(r=>setImmediate(r));initial=false;index=0;const tree=exports.MerchantDashboard(props);const text=JSON.stringify(tree);assert.match(text,/Support offline/);assert.match(text,/INR 100/);assert.match(text,/USD 200/);assert.doesNotMatch(text,/INR 300/);assert.equal(calls.length,4);
 function nodes(value){if(!value||typeof value!=='object')return [];return [value,...Object.values(value).flatMap(nodes)]}
 const cards=nodes(tree).filter(n=>n.type==='button');const support=cards.find(n=>JSON.stringify(n).includes('Open support cases'));assert.match(JSON.stringify(support),/—/);const order=cards.find(n=>JSON.stringify(n).includes('RS-EXACT'));order.props.onClick();assert.deepEqual(destination,['orders','order-exact']);
});
