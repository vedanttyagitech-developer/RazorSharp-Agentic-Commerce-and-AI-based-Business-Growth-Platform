import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function harness(reduced=false){
 const refs=[],deps=[],listeners=new Map(),calls=[];let r=0,e=0,pending=[];
 const exports={};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/use-chat-follow.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports,HTMLTextAreaElement:class{},HTMLInputElement:class{},requestAnimationFrame:f=>{pending.push(f);return 1},cancelAnimationFrame(){},window:{matchMedia:()=>({matches:reduced}),addEventListener:(n,f)=>listeners.set(n,f),removeEventListener:n=>listeners.delete(n)},require:()=>({useRef:v=>refs[r++]??={current:v},useEffect(f,d){const i=e++;if(!deps[i]||d.some((v,j)=>v!==deps[i][j])){deps[i]=d;pending.push(f)}}})});
 return {calls,fire:(n,event)=>listeners.get(n)(event),draw(turn,results='',enabled=true){r=e=0;const nodes=exports.useChatFollow(turn,results,enabled);nodes.latestRef.current={scrollIntoView:o=>calls.push(['turn',o.behavior])};nodes.resultsRef.current={scrollIntoView:o=>calls.push(['results',o.behavior])};while(pending.length){const batch=pending;pending=[];batch.forEach(f=>f())}}};
}
test('new turn moves up smoothly and arriving products become visible',()=>{const h=harness();h.draw('a');h.draw('b');h.draw('b','milk');assert.deepEqual(h.calls.at(-2),['turn','smooth']);assert.deepEqual(h.calls.at(-1),['results','smooth'])});
test('reading older messages stops following until the next request',()=>{const h=harness();h.draw('a');h.fire('wheel',{deltaY:-20});h.draw('a','milk');assert.equal(h.calls.length,1);h.draw('b');assert.equal(h.calls.length,2)});
test('payment overlay suppresses scrolling and reduced motion skips animation',()=>{const h=harness(true);h.draw('a','',false);assert.equal(h.calls.length,0);h.draw('a','',true);assert.deepEqual(h.calls[0],['turn','instant'])});
