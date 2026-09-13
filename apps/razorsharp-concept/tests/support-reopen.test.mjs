import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
test('closed case requires a reason and submits reopening',async()=>{
 const states=[],effects=[],requests=[];let slot=0,es=0,tree;
 const react={useState(initial){const i=slot++;if(!(i in states))states[i]=initial;return [states[i],v=>{states[i]=typeof v==='function'?v(states[i]):v;}];},useRef:current=>({current}),useEffect(fn){const i=es++;if(!effects[i])effects[i]={cleanup:fn()};}};
 const jsx=(type,props)=>({type,props:props??{}}),exports={};
 const code=ts.transpileModule(readFileSync(new URL('../components/live-merchant.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,crypto,URLSearchParams,window:{addEventListener(){},removeEventListener(){}},fetch:async(path,opts)=>{requests.push({path,opts});return {ok:true,json:async()=>({cases:[{case_id:'case',order_id:'order',status:'CLOSED',reason:'item_damaged',created_at:'2026-09-13',updated_at:'2026-09-13'}]})};},require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx}:name==='@/lib/catalogue'?{useCatalogue:()=>({products:[]})}:name==='@/lib/merchant-sync'?{merchantStateChanged(){}}:{}});
 function draw(){slot=es=0;tree=exports.LiveMerchant({view:'support'});}
 function nodes(n){if(!n||typeof n!=='object')return [];if(Array.isArray(n))return n.flatMap(nodes);return [n,...nodes(n.props?.children)];}
 const find=p=>nodes(tree).find(p),flush=async()=>{await new Promise(r=>setImmediate(r));draw();};
 draw();await flush();assert.equal(find(n=>n.type==='button'&&n.props.children==='Reopen case').props.disabled,true);
 find(n=>n.type==='textarea').props.onChange({target:{value:'Closed by mistake'}});await flush();
 const b=find(n=>n.type==='button'&&n.props.children==='Reopen case');assert.equal(b.props.disabled,false);b.props.onClick();await flush();
 const sent=requests.find(r=>r.opts.method==='POST');assert.equal(sent.path,'/api/merchant/support/cases/case/advance');assert.deepEqual(JSON.parse(sent.opts.body),{status:'ACKNOWLEDGED',note:'Closed by mistake'});
});
