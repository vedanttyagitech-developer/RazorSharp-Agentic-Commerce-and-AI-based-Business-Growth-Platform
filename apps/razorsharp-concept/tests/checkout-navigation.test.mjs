import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function load(file){const exports={};vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/'+file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports});return exports}
const {requestsCheckoutReview,checkoutChoice}=load('checkout-choice.ts');
for(const phrase of ['proceed to check out','Okay, proceed to checkout.','Yes, review my bill','check out please','हाँ चेकआउट करो','checkout kar do'])test(`Review navigation: ${phrase}`,()=>assert.equal(requestsCheckoutReview(phrase),true));
for(const phrase of ['do not proceed to checkout','checkout mat karo','how does checkout work?','pay with Reserve Pay','no checkout'])test(`Never infer approval: ${phrase}`,()=>assert.equal(requestsCheckoutReview(phrase),false));
test('UPI remains manual and UAP uses saved authority',()=>{assert.equal(checkoutChoice('pay with UPI'),'manual');assert.equal(checkoutChoice('pay with UAP'),'reserve')});
const {recoveryMessage,canRefreshCheckout,canResumeManualCheckout,needsPaymentAttention}=load('checkout-recovery.ts');
test('Resume uses existing provider order; unknown is not failure',()=>{assert.match(recoveryMessage({state:'AWAITING_PAYMENT',attempt:{razorpay_order_id:'existing',state:'SUBMITTED'}}),/Resume the same/);assert.match(recoveryMessage({state:'PAYMENT_UNKNOWN',attempt:{}}),/awaiting a verified outcome/);assert.equal(canRefreshCheckout({state:'PAYMENT_UNKNOWN',attempt:{},cancellable:true}),false)});
test('Only verified order is called confirmed',()=>{assert.match(recoveryMessage({state:'PAID',order_id:'recorded'}),/confirmed/);assert.match(recoveryMessage({state:'PAYMENT_FAILED'}),/confirmed.*failed/)});
test('Retired checkout with a failed attempt is not an endless pending payment',()=>assert.match(recoveryMessage({state:'INVALIDATED',attempt:{state:'FAILED'}}),/previous payment failed/));
test('Reconciliation sends one stable key for the same checkout',async()=>{
 const exports={},requests=[];
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../lib/commerce.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports,URLSearchParams,fetch:async(url,options)=>{requests.push(options);return {ok:true,text:async()=>JSON.stringify({queued:true})}}});
 await exports.commerce.payments.reconcile('old-checkout');await exports.commerce.payments.reconcile('old-checkout');
 assert.equal(requests.length,2);assert.equal(requests[0].headers['Idempotency-Key'],'provider-recovery:old-checkout');assert.equal(requests[1].headers['Idempotency-Key'],requests[0].headers['Idempotency-Key']);
});

test('Escalated and reconciling attempts cannot reopen a provider payment',()=>{for(const state of ['ESCALATED','RECONCILING','UNKNOWN','FAILED','EXPIRED','CAPTURED'])assert.equal(canResumeManualCheckout({state:'AWAITING_PAYMENT',attempt:{state,razorpay_order_id:'existing'}}),false);assert.match(recoveryMessage({state:'AWAITING_PAYMENT',attempt:{state:'ESCALATED',razorpay_order_id:'existing'}}),/merchant review/)});

for(const state of ['FAILED','EXPIRED'])test(`Resolved ${state} payment leaves homepage recovery, including after reload`,()=>{
 const view={order_id:null,attempt:{state,window_closed:true}};
 assert.equal(needsPaymentAttention(view),false);
 assert.equal(needsPaymentAttention(JSON.parse(JSON.stringify(view))),false);
});
for(const state of ['CREATED','SUBMITTED','AUTHORIZED','UNKNOWN','RECONCILING','ESCALATED','CAPTURED'])test(`Unresolved ${state} payment remains visible even after its window closes`,()=>{
 assert.equal(needsPaymentAttention({order_id:null,attempt:{state,window_closed:true}}),true);
});
test('Confirmed order and reviews without payments need no homepage payment warning',()=>{
 assert.equal(needsPaymentAttention({order_id:'order',attempt:{state:'CAPTURED'}}),false);
 assert.equal(needsPaymentAttention({order_id:null,attempt:null}),false);
});
test('Closed window cannot mask verified failure or merchant escalation',()=>{
 assert.match(recoveryMessage({state:'PAYMENT_FAILED',attempt:{state:'FAILED',window_closed:true}}),/confirmed.*failed/);
 assert.match(recoveryMessage({state:'AWAITING_PAYMENT',attempt:{state:'ESCALATED',window_closed:true}}),/merchant review/);
});
for(const phrase of ['Checkout pe chal','checkout par chalo','Ab checkout pe chaliye','चेकआउट पे चलो','review','review karo','बिल दिखाओ'])test(`Conversational checkout navigation: ${phrase}`,()=>assert.equal(requestsCheckoutReview(phrase),true));
for(const phrase of ['checkout pe mat chal','review nahi karo','what is review?'])test(`Do not navigate for refusal/question: ${phrase}`,()=>assert.equal(requestsCheckoutReview(phrase),false));

const {shopNavigation}=load('shop-navigation.ts');
for(const phrase of ['show me my basket','open cart','mera cart dikhao','मेरा कार्ट दिखाओ'])test(`Basket navigation: ${phrase}`,()=>assert.equal(shopNavigation(phrase),'basket'));
for(const phrase of ['cancel checkout','cancel order review','close review','checkout band karo'])test(`Close surface: ${phrase}`,()=>assert.equal(shopNavigation(phrase),'close-review'));
for(const phrase of ['cancel my order','cancel payment','do not close checkout','how do I cancel checkout'])test(`Not surface navigation: ${phrase}`,()=>assert.equal(shopNavigation(phrase),null));
