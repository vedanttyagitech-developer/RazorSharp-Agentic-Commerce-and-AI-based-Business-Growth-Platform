import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

test('readiness clears old healthy evidence when the next health request fails',async()=>{
 const state=[],effects=[];let slot=0,es=0,tree,poll,failed=false;
 const jsx=(type,props)=>({type,props:props||{}});
 const api=async()=>{if(failed)throw Error('offline');return {api:{status:'healthy'},worker:{status:'unavailable'},voice:{status:'degraded'}};};
 const react={useState(initial){const i=slot++;if(!(i in state))state[i]=initial;return [state[i],v=>{state[i]=v;}];},useEffect(fn,deps){const i=es++;if(!effects[i])effects[i]={cleanup:fn(),deps};}};
 const exports={};
 const code=ts.transpileModule(readFileSync(new URL('../components/runtime-readiness.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,setInterval:fn=>{poll=fn;return 1;},clearInterval:()=>{},require:name=>name==='react'?react:{jsx,jsxs:jsx}});
 const draw=()=>{slot=es=0;tree=exports.RuntimeReadiness({api,refreshKey:'1'});};
 draw();await new Promise(r=>setImmediate(r));draw();
 assert.match(JSON.stringify(tree),/healthy/);assert.match(JSON.stringify(tree),/unavailable/);assert.match(JSON.stringify(tree),/degraded/);
 failed=true;poll();await new Promise(r=>setImmediate(r));draw();
 assert.doesNotMatch(JSON.stringify(tree),/healthy/);assert.match(JSON.stringify(tree),/unknown/);
 effects.forEach(e=>e.cleanup?.());
});
