import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function load(path,imports={}){const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL(path,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,Set,require:n=>imports[n]??{}});return exports;}
const recovery=load('../lib/checkout-recovery.ts');
const {isFailedPayment}=load('../components/previous-payments.tsx',{'@/lib/checkout-recovery':recovery});
for(const state of ['FAILED','EXPIRED'])test(`${state} belongs to failed payments, never pending`,()=>{const view={state:'PAYMENT_FAILED',attempt:{state},order_id:null};assert.equal(isFailedPayment(view),true);assert.equal(recovery.needsPaymentAttention(view),false);});
for(const state of ['UNKNOWN','ESCALATED','SUBMITTED','AUTHORIZED','CAPTURED'])test(`${state} remains recoverable even when checkout looks failed`,()=>{const view={state:'PAYMENT_FAILED',attempt:{state},order_id:null};assert.equal(isFailedPayment(view),false);assert.equal(recovery.needsPaymentAttention(view),true);});
test('confirmed order is excluded from both payment attention lists',()=>{const view={state:'PAID',attempt:{state:'FAILED'},order_id:'confirmed-order'};assert.equal(isFailedPayment(view),false);assert.equal(recovery.needsPaymentAttention(view),false);});
