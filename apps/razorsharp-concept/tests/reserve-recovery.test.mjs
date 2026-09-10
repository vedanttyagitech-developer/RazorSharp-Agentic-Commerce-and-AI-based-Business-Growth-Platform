import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function load(read){const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/reserve-recovery.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports,require:()=>({commerce:read})});return exports.reserveRequestSettled}
test('lost submission response resolves original checkout and captured order',async()=>{
 const paths=[];const settled=load(async p=>{paths.push(p);return paths.length===1?{checkout_id:'old',order_id:'order',attempt:{attempt_id:'attempt'}}:{status:'CAPTURED',order_id:'order',allocation:'SPENT'}});
 assert.equal(await settled('old'),true);assert.deepEqual(paths,['checkouts/old','reserve/payments/attempt']);
});
for(const [status,allocation,expected] of [['UNKNOWN','HELD',false],['FAILED','HELD',false],['FAILED','RELEASED',true],['EXPIRED','RELEASED',true],['EXPIRED','HELD',false],['CAPTURED','SPENT',false]]){
 test(`${status}/${allocation} cannot clear an unverified successful order`,async()=>{
 const settled=load(async p=>p.startsWith('checkouts/')?{checkout_id:'old',attempt:{attempt_id:'a'}}:{status,allocation,order_id:null});
 assert.equal(await settled('old'),expected);
 });
}
test('network failure preserves pending marker',async()=>{await assert.rejects(load(async()=>{throw Error('offline')})('old'),/offline/)});
test('missing attempt is not terminal',async()=>{assert.equal(await load(async()=>({checkout_id:'old'}))('old'),false)});
