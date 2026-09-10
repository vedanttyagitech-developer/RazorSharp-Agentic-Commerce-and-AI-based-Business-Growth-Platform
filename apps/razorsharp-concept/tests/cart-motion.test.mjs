import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function load(paused=false){
 const exports={},animations=[],appended=[];
 const ghost={style:{},setAttribute(){},remove(){},animate(frames,options){animations.push({frames,options});return {cancel(){},finished:Promise.resolve()}}};
 const image={cloneNode:()=>ghost,getBoundingClientRect:()=>({left:20,top:80,width:100,height:100})};
 const source={querySelector:()=>image};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/continuity.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,require:()=>({}),matchMedia:()=>({matches:paused}),document:{documentElement:{dataset:{}},querySelector:()=>({getBoundingClientRect:()=>({left:300,top:20})}),body:{appendChild:n=>appended.push(n)}},setTimeout:()=>0});
 return {api:exports,image,source,animations,appended};
}
test('flight retains geometry after acknowledgement replaces original product cards',()=>{
 const h=load();const origin=h.api.captureProductMotion(h.source);h.image.getBoundingClientRect=()=>{throw Error('Original product is gone')};h.api.flyToBasket(origin);assert.equal(h.appended.length,1);assert.equal(h.animations.length,1);assert.equal(h.appended[0].style.left,'20px');
});
test('removal keeps its image after removing the original line',()=>{
 const h=load();const origin=h.api.captureProductMotion(h.source);h.source.querySelector=()=>null;h.api.dropFromBasket(origin);assert.equal(h.animations.length,1);
});
test('reduced motion preference is preserved',()=>{
 const h=load(true);h.api.flyToBasket(h.api.captureProductMotion(h.source));assert.equal(h.animations.length,0);
});
