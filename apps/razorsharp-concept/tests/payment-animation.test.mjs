import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import ts from 'typescript';
import {renderToStaticMarkup} from 'react-dom/server';
const require=createRequire(import.meta.url);
const productExports={};
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/reserve-confirmed-items.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:productExports,require(name){if(name==='next/image')return {default:({unoptimized:_unoptimized,...props})=>require('react').createElement('img',props)};return require(name)}});
const exports={};
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/payment-animation.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports,require(name){if(name==='./payment-acknowledgement')return {PaymentAcknowledgement:({orderId})=>require('react').createElement('section',{'data-receipt-order':orderId})};if(name==='./reserve-confirmed-items')return productExports;if(name==='./reserve-payment-frame')return {ReservePaymentFrame:({children})=>children};if(name.endsWith('.css'))return {};if(name==='@/lib/demo')return {money:n=>`INR ${n/100}`};return require(name)}});
const {PaymentAnimation,paymentAnimationState}=exports;
test('completion requires captured status and a recorded order, not a callback or elapsed animation',()=>{
 for(const state of ['CAPTURED','PAID','CONFIRMED']){assert.equal(paymentAnimationState(state,null),'verifying');assert.equal(paymentAnimationState(state,'order-1'),'completed')}
 assert.equal(paymentAnimationState('PROCESSING','order-1'),'processing');
});
test('uncertain, escalated and disconnected payments never celebrate',()=>{
 for(const state of ['UNKNOWN','ESCALATED','RECONCILING']){assert.equal(paymentAnimationState(state,null),'attention');const html=renderToStaticMarkup(PaymentAnimation({state,method:'reserve',amount:5750}));assert.doesNotMatch(html,/payment-motion-confetti|Payment completed/);assert.match(html,/do not pay again/)}
 assert.equal(paymentAnimationState('QUEUED',null,true),'attention');
 const offline=renderToStaticMarkup(PaymentAnimation({state:'PROCESSING',interrupted:true,caption:'Complete payment on the provider screen'}));assert.doesNotMatch(offline,/Complete payment on the provider screen/);assert.match(offline,/do not pay again/);
});
test('failed and expired attempts retain failure state without a completion mark',()=>{
 for(const state of ['FAILED','EXPIRED','CANCELLED']){const html=renderToStaticMarkup(PaymentAnimation({state}));assert.match(html,/Payment not completed/);assert.doesNotMatch(html,/payment-motion-confetti/)}
});
test('confirmed Reserve payment shows amount and clearly labels simulator',()=>{
 const html=renderToStaticMarkup(PaymentAnimation({state:'CAPTURED',orderId:'order-1',amount:5750,method:'reserve'}));assert.match(html,/Payment completed/);assert.match(html,/INR 57.5/);assert.match(html,/SIMULATED PROVIDER/);assert.match(html,/Payment completed/);
});

test('Reserve flow does not show authorisation before admission or capture before confirmation',()=>{
 const queued=renderToStaticMarkup(PaymentAnimation({state:'PROCESSING',method:'reserve',admitted:false}));
 assert.match(queued,/Checking this exact purchase/);assert.doesNotMatch(queued,/Exact purchase authorised|transfer-complete/);
 const admitted=renderToStaticMarkup(PaymentAnimation({state:'SUBMITTED',method:'reserve',admitted:true}));
 assert.match(admitted,/Execution attempt recorded/);assert.match(admitted,/awaiting provider evidence/);assert.doesNotMatch(admitted,/transfer-complete|Simulated capture recorded/);
});
test('Razorpay keeps its own animation and does not render Reserve transfer nodes',()=>{
 const html=renderToStaticMarkup(PaymentAnimation({state:'SUBMITTED',method:'razorpay'}));
 assert.match(html,/RAZORPAY CHECKOUT/);assert.doesNotMatch(html,/transfer-wallet|Merchant account|UCP/);
});

const purchased={orderId:'order-1',reference:'RS-actual',items:[{sku:'milk',name:'Milk',quantity:2,image:'/products/milk.webp'},{sku:'bread',name:'Bread',quantity:1,image:'/products/bread.webp'},{sku:'extra',name:'No artwork product',quantity:3}]};
test('Reserve confirmation displays all purchased images and backend quantities inside the scene',()=>{
 const html=renderToStaticMarkup(PaymentAnimation({state:'CAPTURED',orderId:'order-1',method:'reserve',confirmedOrder:purchased}));
 assert.match(html,/Confirmed order items/);assert.match(html,/6 items/);assert.match(html,/RS-actual/);
 for(const item of purchased.items){assert.match(html,new RegExp(item.name));assert.match(html,new RegExp(`Quantity ${item.quantity}`));if(item.image)assert.ok(html.includes(`src="${item.image}"`));}
 assert.match(html,/Image unavailable/);assert.match(html,/data-receipt-order="order-1"/);
 assert.equal((html.match(/class="confirmed-item-card"/g)||[]).length,3);
});
test('products never celebrate an unconfirmed or mismatched payment',()=>{
 for(const props of [{state:'PROCESSING',orderId:'order-1'},{state:'UNKNOWN',orderId:'order-1'},{state:'FAILED',orderId:'order-1'},{state:'CAPTURED',orderId:null},{state:'CAPTURED',orderId:'different-order'}]){
 const html=renderToStaticMarkup(PaymentAnimation({method:'reserve',confirmedOrder:purchased,...props}));assert.doesNotMatch(html,/confirmed-item-card|YOUR FINDS, CONFIRMED|data-receipt-order/);
 }
});

test('confirmed Razorpay orders get the same purchased items and receipt without Reserve branding',()=>{
 const html=renderToStaticMarkup(PaymentAnimation({state:'CAPTURED',orderId:'order-1',method:'razorpay',confirmedOrder:purchased}));
 assert.match(html,/RAZORPAY CHECKOUT/);assert.match(html,/confirmed-item-card/);assert.match(html,/data-receipt-order="order-1"/);assert.doesNotMatch(html,/SIMULATED PROVIDER/);
});

test('confirmation hands over to one bag and one receipt, without processing graphics',()=>{
 for(const method of ['reserve','razorpay']){
  const html=renderToStaticMarkup(PaymentAnimation({state:'CAPTURED',orderId:'order-1',method,confirmedOrder:purchased}));
  assert.match(html,/Order successful/);
  assert.match(html,/order-success-columns/);
  assert.equal((html.match(/data-receipt-order=/g)||[]).length,1);
  assert.doesNotMatch(html,/centre-payment-orbit|centre-admission-note|reserve-transfer-status/);
 }
});

test('completed journey removes the processing bill and Kernel panels',()=>{
 const journey={};
 vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../components/payment-journey.tsx',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:journey,require(name){if(name.endsWith('.css'))return {};if(name==='@/lib/demo')return {money:n=>`INR ${n/100}`};return require(name)}});
 const html=renderToStaticMarkup(journey.PaymentJourney({amount:5750,method:'reserve',phase:'completed',children:require('react').createElement('div',null,'Confirmed contents')}));
 assert.match(html,/Confirmed contents/);assert.doesNotMatch(html,/Exact purchase bill|Kernel flow|journey-bill/);
});
