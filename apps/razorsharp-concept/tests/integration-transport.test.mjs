import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
// Exercise the provider boundary without simulating a confirmed backend capture.
for (const ending of ['dismissed', 'reported', 'failed']) test(`Razorpay hides contact and preserves the handoff on ${ending}`, async () => {
 let options, failed;
 const report = {razorpay_payment_id:'pay_test',razorpay_order_id:'order_test',razorpay_signature:'test-signature'};
 class Checkout {
  constructor(value) { options = value; }
  on(name, callback) { assert.equal(name, 'payment.failed'); failed = callback; }
  open() {
   if (ending === 'reported') options.handler(report);
   if (ending === 'failed') failed({error:{code:'BAD_REQUEST_ERROR',description:'Test decline'}});
   options.modal.ondismiss(); // A subsequent dismissal must not overwrite the first event.
  }
 }
 const api = load('lib/razorpay.ts', () => { throw Error('No app-side payment request'); }, {window:{Razorpay:Checkout}});
 const result = await api.openRazorpay({keyId:'rzp_test_fixture',orderId:'order_test',amountMinor:5750,currency:'INR',merchantName:'Test merchant',description:'Reviewed purchase'});
 assert.equal(options.hidden.contact, true);
 assert.equal(options.prefill.name, 'Vedant Tyagi');
 assert.equal(options.prefill.contact, '+919876543210');
 assert.equal(options.notes.demo_billed_to, 'Vedant Tyagi');
 assert.equal(options.order_id, 'order_test');
 assert.equal(options.amount, 5750);
 assert.equal(options.currency, 'INR');
 assert.equal(options.retry.enabled, false);
 assert.equal(result.kind, ending);
 if (ending === 'reported') assert.equal(result.report, report);
 if (ending === 'failed') assert.equal(result.code, 'BAD_REQUEST_ERROR');
});
function load(path,fetch,extra={}){const exports={};const code=ts.transpileModule(readFileSync(new URL('../'+path,import.meta.url),'utf8'),{fileName:path,compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{requestAnimationFrame:fn=>{fn();return 0},cancelAnimationFrame:()=>{},exports,fetch,crypto,performance,URL,URLSearchParams,Response,Request,Headers,AbortController,process:{env:{}},require:()=>({}),console,...extra});return exports}
test('A stalled provider frame has an explicit escape, and repeated open shares one checkout',async()=>{
 let opens=0,closes=0,options,timer,button,removed=0;
 class Checkout {constructor(o){options=o}on(){}open(){opens++}close(){closes++;options.modal.ondismiss()}}
 const doc={querySelectorAll:()=>[],createElement:()=>({setAttribute(){},style:{},remove(){removed++}}),body:{appendChild:b=>{button=b}}};
 const api=load('lib/razorpay.ts',()=>{throw Error('No payment mutation')},{window:{Razorpay:Checkout},document:doc,setTimeout:fn=>{timer=fn;return 1},clearTimeout:()=>{}});
 const h={keyId:'rzp_test_fixture',orderId:'same',amountMinor:5750,currency:'INR',merchantName:'Test',description:'Test'};
 const first=api.openRazorpay(h);await Promise.resolve();const second=api.openRazorpay(h);await Promise.resolve();
 await assert.rejects(api.openRazorpay({...h,orderId:'different'}),/already open/);
 assert.equal(opens,1);assert.equal(closes,0);timer();assert.match(button.textContent,/payment status/);assert.equal(closes,0);
 button.onclick();assert.equal((await first).kind,'dismissed');assert.equal((await second).kind,'dismissed');assert.equal(closes,1);assert.equal(removed,1);
});
test('A hung SDK load times out and permits a fresh load instead of caching rejection',async()=>{
 let timeout,tag,loads=0,removed=0;
 const win={setTimeout:fn=>{timeout=fn;return 1},clearTimeout:()=>{}};
 const doc={querySelectorAll:()=>[],createElement:()=>({remove(){removed++}}),head:{appendChild:t=>{tag=t;loads++}}};
 const api=load('lib/razorpay.ts',()=>{},{window:win,document:doc});
 const pending=api.loadRazorpay();timeout();await assert.rejects(pending,/could not be reached/);assert.equal(removed,1);
 const retry=api.loadRazorpay();win.Razorpay=class Checkout{};tag.onload();assert.equal(await retry,win.Razorpay);assert.equal(loads,2);
});
test('Live and unrecognised keys never receive demo identity',async()=>{
 let options;
 class Checkout {constructor(value){options=value}on(){}open(){options.modal.ondismiss()}}
 const api=load('lib/razorpay.ts',()=>{throw Error('Unexpected request')},{window:{Razorpay:Checkout}});
 for(const keyId of ['rzp_live_fixture','rzp_testish']){
  await api.openRazorpay({keyId,orderId:'order_live',amountMinor:5750,currency:'INR',merchantName:'Merchant',description:'Purchase'});
  assert.equal(options.prefill,undefined);assert.equal(options.notes,undefined);
 }
});
test('Delayed capture stays unresolved until the backend returns an order',async()=>{
 let now=0,reads=0,slow=0;
 const views=[{state:'AWAITING_PAYMENT'},{state:'PAYMENT_UNKNOWN'},{state:'AWAITING_PAYMENT'},{state:'CONFIRMED',order_id:'confirmed-order',order_reference:'RS-test'}];
 const commerce={checkout:{read:async id=>{assert.equal(id,'same-checkout');return views[reads++]}}};
 const api=load('lib/manual-pay.ts',()=>{throw Error('No payment mutation during polling')},{require:()=>({commerce}),Date:{now:()=>now},setTimeout:fn=>{now+=21000;queueMicrotask(fn);return 1}});
 const result=await api.awaitSettlement('same-checkout',{onSlow:()=>slow++});
 assert.equal(reads,4);assert.equal(slow,1);assert.equal(result.kind,'confirmed');assert.equal(result.orderId,'confirmed-order');
});
test('A connection failure during settlement preserves pending approval for recovery',async()=>{
 let stored=JSON.stringify({checkoutId:'same-checkout',keys:{approve:'original',verify:'original-verify'}});
 const commerce={checkout:{read:async()=>{throw Error('connection lost')}}};
 const api=load('lib/manual-pay.ts',()=>{},{require:()=>({commerce,CommerceError:Error}),sessionStorage:{getItem:()=>stored,removeItem:()=>{stored=null}}});
 await assert.rejects(api.awaitSettlement('same-checkout'),/connection lost/);
 assert.equal(api.readPending().keys.approve,'original');
});
test('Only a backend terminal failure settles a payment as failed',async()=>{
 const api=load('lib/manual-pay.ts',()=>{},{require:()=>({commerce:{checkout:{read:async()=>({state:'PAYMENT_FAILED',order_id:null})}}})});
 const result=await api.awaitSettlement('same-checkout');assert.equal(result.kind,'failed');assert.equal(result.state,'PAYMENT_FAILED');
});
test('Transient disconnect retries the same checkout and reconciles before confirmed capture',async()=>{
 const {CommerceError}=load('lib/commerce.ts',()=>{});
 let reads=0,now=1;const connections=[],delays=[],reconciles=[];
 const commerce={checkout:{read:async id=>{assert.equal(id,'same-checkout');reads++;if(reads<3)throw new CommerceError(503,'Offline','Connection lost');return reads===3?{state:'AWAITING_PAYMENT'}:{state:'PAID',order_id:'verified-order'}}},payments:{reconcile:async id=>reconciles.push(id)}};
 const api=load('lib/manual-pay.ts',()=>{throw Error('No new payment');},{require:()=>({commerce,CommerceError}),Date:{now:()=>now},setTimeout:(fn,ms)=>{delays.push(ms);now+=ms;queueMicrotask(fn);return 1}});
 const result=await api.awaitSettlement('same-checkout',{reconcile:true,onConnectionChange:online=>connections.push(online)});
 assert.equal(result.orderId,'verified-order');assert.deepEqual(connections,[false,true]);assert.deepEqual(delays,[1500,3000,1500]);assert.deepEqual(reconciles,['same-checkout']);
});
test('Recovery does not hide an authentication refusal as a temporary disconnect',async()=>{
 const {CommerceError}=load('lib/commerce.ts',()=>{});
 const api=load('lib/manual-pay.ts',()=>{},{require:()=>({CommerceError,commerce:{checkout:{read:async()=>{throw new CommerceError(401,'Sign in','Session expired')}}}})});
 await assert.rejects(api.awaitSettlement('same-checkout'),error=>error.status===401);
});
const reply=(body,status=200)=>Response.json(body,{status});
test('Reserve history ignores only non-Reserve orders',async()=>{let i=0;const api=load('lib/reserve-api.ts',async()=>[reply({orders:[{order_id:'one',payment_attempt_id:'a'},{order_id:'two',payment_attempt_id:'b'}]}),reply({allocation:'CONSUMED',status:'CAPTURED',authority_id:'authority'}),reply({detail:'not Reserve'},404)][i++]);const rows=await api.reservePurchases();assert.equal(rows.length,1);assert.equal(rows[0].order_id,'one')});
for(const status of [401,403,500,503])test(`Reserve history surfaces ${status}`,async()=>{let i=0;const api=load('lib/reserve-api.ts',async()=>i++===0?reply({orders:[{payment_attempt_id:'a'}]}):reply({detail:'unavailable'},status));await assert.rejects(api.reservePurchases(),error=>error.status===status)});
for(const status of [401,409])test(`Voice ticket recovers ${status} through the buyer bridge once`,async()=>{const calls=[];const api=load('lib/voice/client.ts',async url=>{calls.push(url);return calls.length===1?reply({},status):url.includes('carts/current')?reply({cart:null}):reply({ticket:'ticket'})});assert.equal((await api.VoiceClient.ticket()).ticket,'ticket');assert.deepEqual(calls,['/api/voice/ticket','/api/commerce/carts/current','/api/voice/ticket'])});
test('Voice does not retry an infrastructure failure',async()=>{let count=0;const api=load('lib/voice/client.ts',async()=>{count++;return reply({detail:'offline'},503)});await assert.rejects(api.VoiceClient.ticket(),/offline/);assert.equal(count,1)});
test('Voice ticket rejects cross-origin writes before reaching gateway',async()=>{let calls=0;const route=load('app/api/voice/ticket/route.ts',async()=>{calls++;return reply({})});const result=await route.POST(new Request('http://localhost:3000/api/voice/ticket',{method:'POST',headers:{Origin:'https://other.example'}}));assert.equal(result.status,403);assert.equal(calls,0)});

test('Merchant bridge does not elevate buyer cookies',async()=>{let calls=0;const route=load('app/api/merchant/[...path]/route.ts',async()=>{calls++;return reply({})});const result=await route.GET(new Request('http://localhost:3000/api/merchant/support/cases',{headers:{Cookie:'rs_buyer_token=buyer'}}),{params:Promise.resolve({path:['support','cases']})});assert.equal(result.status,401);assert.equal(calls,0)});
test('Merchant bridge refuses financial routes',async()=>{let calls=0;const route=load('app/api/merchant/[...path]/route.ts',async()=>{calls++;return reply({})});const result=await route.POST(new Request('http://localhost:3000/api/merchant/refunds',{method:'POST',headers:{Origin:'http://localhost:3000'}}),{params:Promise.resolve({path:['refunds']})});assert.equal(result.status,404);assert.equal(calls,0)});
test('Merchant login is bound to MERCHANT and hides credentials from body',async()=>{let actor;const route=load('app/api/merchant/[...path]/route.ts',async(_url,options)=>{actor=JSON.parse(options.body).actor_type;return reply({token:'test-only-token'})});const result=await route.POST(new Request('http://localhost:3000/api/merchant/session',{method:'POST',headers:{Origin:'http://localhost:3000','Content-Type':'application/json'},body:JSON.stringify({key:'fixture-only-key',actor_type:'OPERATOR'})}),{params:Promise.resolve({path:['session']})});assert.equal(actor,'MERCHANT');assert.deepEqual(await result.json(),{authenticated:true});assert.match(result.headers.get('set-cookie'),/HttpOnly/)});

test('HTTP assistant projection accepts product contracts and does not infer products from prose',()=>{const api=load('lib/agent-turn.ts',()=>{});assert.equal(api.projectTurn({reply:'milk costs 20'}).items.length,0);assert.equal(api.projectTurn({kind:'product',sku:'milk'}).items[0].sku,'milk');assert.equal(api.projectTurn({kind:'product',product:{sku:'bread'}}).items[0].sku,'bread')});
test('HTTP assistant only passes explicit basket proposals, including removal',()=>{const api=load('lib/agent-turn.ts',()=>{});assert.equal(api.projectTurn({kind:'product',sku:'milk'}).proposal,null);assert.equal(api.projectTurn({proposal:{action:'basket.update',sku:'milk',delta:-1,display:{name:'Milk'}}}).proposal.quantity,-1);assert.equal(api.projectTurn({proposal:{action:'refund',sku:'milk',delta:1}}).proposal,null)});

test('Voice waits for server end AND drained audio before resuming, across turns',async()=>{
 const sent=[];
 const conversation=load('lib/voice/conversation.ts',()=>{});
 const api=load('lib/voice/client.ts',()=>{},{require:name=>name==='./conversation'?conversation:name==='./wire'?{VOICE_PROTOCOL_VERSION:2}:{Microphone:class{},SpeechPlayer:class{}},WebSocket:{OPEN:1}});
 const client=new api.VoiceClient();client.socket={readyState:1,send:frame=>sent.push(JSON.parse(frame))};
 for(const id of [1,2]){
  await client.receive({data:JSON.stringify({type:'speech_start',utterance_id:id,speech_generation:0})});
  client.outputs.get(id).audible=true;
  client.playbackEnded(id);
  assert.equal(sent.length,id-1,'A gap before speech_end must not acknowledge completion');
  await client.receive({data:JSON.stringify({type:'speech_end',utterance_id:id,speech_generation:0})});
  client.playbackEnded(id);
  assert.equal(sent.length,id,'Exactly one completion per utterance');
  assert.equal(sent[id-1].utterance_id,id);
 }
});

test('Failed checkout recovery stops before creating another cart or reservation',async()=>{
 const states=[];let creates=0;
 class CommerceError extends Error{constructor(status,message){super(message);this.status=status;this.unreachable=false}}
 const failure=new CommerceError(503,'Recovery service unavailable');
 const commerce={checkout:{list:async()=>{throw failure}},cart:{create:async()=>{creates++;throw Error('Must not create')}}};
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:initial=>[initial,value=>states.push(value)],useRef:value=>({current:value}),useCallback:fn=>fn,useEffect:fn=>fn()};
 const api=load('lib/authoritative-bill.ts',()=>{},{AbortController,require:name=>name==='react'?hooks:{commerce,CommerceError,idempotencyKey:()=>crypto.randomUUID()}});
 api.useAuthoritativeBill([{sku:'milk',quantity:1}],true);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(creates,0);
 assert.equal(states.at(-1).status,'unavailable');
 assert.equal(states.at(-1).error,failure);
 assert.equal(states.at(-1).retryable,true);
});

test('Durable cart serializes additions and replays an unknown write with the same key',async()=>{
 let stored={cart_id:'one',lines:[{sku:'milk',quantity:1}]};let fail=true;const writes=[];
 const api={cart:{current:async()=>({cart:stored}),read:async()=>structuredClone(stored),setLine:async(id,sku,quantity,key)=>{writes.push({id,quantity,key});stored={cart_id:id,lines:[{sku,quantity}]};if(fail){fail=false;throw Object.assign(Error('lost response'),{status:503})}return structuredClone(stored)}},checkout:{list:async()=>({checkouts:[]})}};
 const {DurableCart}=load('lib/durable-cart.ts',()=>{},{require:()=>({commerce:api,idempotencyKey:()=>crypto.randomUUID()})});
 const cart=new DurableCart(()=>{});await cart.restore();await assert.rejects(cart.change('milk',1));await assert.rejects(cart.change('milk',1),/retry/);await cart.retry();assert.equal(writes[0].key,writes[1].key);assert.equal(stored.lines[0].quantity,2);
 await Promise.all([cart.change('milk',1),cart.change('milk',1)]);assert.equal(stored.lines[0].quantity,4);
 const reloaded=new DurableCart(()=>{});await reloaded.restore();assert.equal(reloaded.cart.lines[0].quantity,4);
});
test('Durable cart forwards proposal binding and rejects a different cart',async()=>{
 const binding={basket_content_hash:'hash',unit_price_minor:200,catalogue_revision:1};let args;
 const record={cart_id:'one',lines:[]};const api={cart:{current:async()=>({cart:record}),read:async()=>record,setLine:async(...input)=>{args=input;return record}}};
 const {DurableCart}=load('lib/durable-cart.ts',()=>{},{require:()=>({commerce:api,idempotencyKey:()=>crypto.randomUUID()})});const cart=new DurableCart(()=>{});await cart.restore();await assert.rejects(cart.change('milk',1,{cartId:'other'}),/another cart/);await cart.change('milk',1,{cartId:'one',absoluteQuantity:3,binding});assert.equal(args[2],3);assert.equal(args[5],binding);
});
test('First add recovers the canonical cart before applying a bound proposal',async()=>{
 const record={cart_id:'canonical',lines:[]};let creates=0,writes=0;
 const api={cart:{current:async()=>({cart:record}),create:async()=>{creates++;throw Error('must not fork cart')},read:async()=>record,setLine:async()=>{writes++;return record}}};
 const {DurableCart}=load('lib/durable-cart.ts',()=>{},{require:()=>({commerce:api,idempotencyKey:()=>crypto.randomUUID()})});
 const cart=new DurableCart(()=>{});
 await cart.change('milk',1,{cartId:'canonical',absoluteQuantity:1});
 assert.equal(creates,0);assert.equal(writes,1);assert.equal(cart.cart.cart_id,'canonical');
 await assert.rejects(cart.change('milk',1,{cartId:'stale'}),/another cart/);assert.equal(writes,1);
});
test('Review reuses the durable cart without copying its lines to another cart',async()=>{
 const states=[];let created=0,rewritten=0,checked;
 const record={cart_id:'durable',lines:[{sku:'milk',quantity:2}],unavailable:[]};
 const commerce={checkout:{list:async()=>({checkouts:[]})},cart:{read:async()=>record,create:async()=>{created++},setLine:async()=>{rewritten++},checkout:async(id)=>{checked=id;return {checkout_id:'review'}}}};
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:initial=>[initial,value=>states.push(value)],useRef:value=>({current:value}),useCallback:fn=>fn,useEffect:fn=>fn()};
 const api=load('lib/authoritative-bill.ts',()=>{},{AbortController,require:name=>name==='react'?hooks:{commerce,CommerceError:Error,idempotencyKey:()=>crypto.randomUUID()}});
 api.useAuthoritativeBill(record.lines,true,'durable');await new Promise(resolve=>setImmediate(resolve));assert.equal(created,0);assert.equal(rewritten,0);assert.equal(checked,'durable');assert.equal(states.at(-1).status,'ready');
});

test('Reload of an admitted checkout recovers it without creating a new purchase',async()=>{
 const states=[];let created=0;
 const view={checkout_id:'existing',cart_id:'durable',state:'PAYMENT_UNKNOWN',approval_card:null,order_id:null};
 const commerce={checkout:{list:async()=>({checkouts:[view]}),read:async()=>view},cart:{create:async()=>created++,read:async()=>{throw Error('must not recreate a closed cart')}}};
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:initial=>[initial,value=>states.push(value)],useRef:value=>({current:value}),useCallback:fn=>fn,useEffect:fn=>fn()};
 const api=load('lib/authoritative-bill.ts',()=>{},{AbortController,require:name=>name==='react'?hooks:{commerce,CommerceError:Error,idempotencyKey:()=>crypto.randomUUID()}});
 api.useAuthoritativeBill([{sku:'milk',quantity:1}],true,'durable');await new Promise(resolve=>setImmediate(resolve));assert.equal(created,0);assert.equal(states.at(-1).status,'recovering');assert.equal(states.at(-1).view.checkout_id,'existing');
});

test('Recovery does not attach another carts confirmed order to the reviewed basket',async()=>{
 const states=[],read=[];
 const own={checkout_id:'own',cart_id:'my-cart',state:'AWAITING_PAYMENT',order_id:null};
 const other={checkout_id:'other',cart_id:'other-cart',state:'CONFIRMED',order_id:'other-order'};
 const commerce={checkout:{list:async()=>({checkouts:[other,own]}),read:async id=>{read.push(id);return id==='own'?own:other}},cart:{create:async()=>{throw Error('No new cart')},read:async()=>{throw Error('No cart recreation')}}};
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:initial=>[initial,value=>states.push(value)],useRef:value=>({current:value}),useCallback:fn=>fn,useEffect:fn=>fn()};
 const api=load('lib/authoritative-bill.ts',()=>{},{AbortController,require:name=>name==='react'?hooks:{commerce,CommerceError:Error,idempotencyKey:()=>crypto.randomUUID()}});
 api.useAuthoritativeBill([{sku:'milk',quantity:1}],true,'my-cart');await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(read,['own']);assert.equal(states.at(-1).view.checkout_id,'own');assert.equal(states.at(-1).view.order_id,null);
});
test('Manual payment persists its exact approval key before an unreachable response',async()=>{
 let stored;const sessionStorage={getItem:()=>stored??null,setItem:(_k,v)=>stored=v,removeItem:()=>stored=null};
 const commerce={checkout:{approveAndPay:async()=>{assert.ok(stored);throw Error('offline')}}};
 const api=load('lib/manual-pay.ts',()=>{},{sessionStorage,require:()=>({commerce,admitted:x=>x,CommerceError:Error})});
 const card={checkout_id:'same',version:1,content_hash:'hash',amount_minor:5750,currency:'INR'};
 const pending=api.pendingFor(card);await assert.rejects(api.approve(pending),/offline/);
 assert.equal(api.pendingFor(card).keys.approve,pending.keys.approve);
});

test('Merchant refund bridge refuses buyer cookies even on allowed order route',async()=>{
 let calls=0;const route=load('app/api/merchant/[...path]/route.ts',async()=>{calls++;return reply({})});
 const path=['orders','00000000-0000-0000-0000-000000000001','refunds'];
 const result=await route.POST(new Request('http://localhost:3000/api/merchant/'+path.join('/'),{method:'POST',headers:{Origin:'http://localhost:3000',Cookie:'rs_buyer_token=buyer'},body:'{}'}),{params:Promise.resolve({path})});
 assert.equal(result.status,401);assert.equal(calls,0);
});
test('Merchant refund bridge forwards exact approval and stable key',async()=>{
 let forwarded;const route=load('app/api/merchant/[...path]/route.ts',async(url,options)=>{forwarded={url,...options};return reply({decision:{allowed:true}})});
 const path=['orders','00000000-0000-0000-0000-000000000001','refunds'];const body=JSON.stringify({amount_minor:100,case_id:'case',approval_hash:'hash',reason:'item_damaged'});
 const result=await route.POST(new Request('http://localhost:3000/api/merchant/'+path.join('/'),{method:'POST',headers:{Origin:'http://localhost:3000',Cookie:'rs_merchant_token=merchant-fixture; rs_merchant_key=scenario-fixture','Idempotency-Key':'stable-fixture'},body}),{params:Promise.resolve({path})});
 assert.equal(result.status,200);assert.equal(forwarded.body,body);assert.equal(forwarded.headers['Idempotency-Key'],'stable-fixture');assert.equal(forwarded.headers.Authorization,'Bearer merchant-fixture');
});

test('Demo refund requires escalation and returns only reported item paid amount once',()=>{
 const {DEMO_ORDERS,changeDemoOrder}=load('lib/simulated-orders.ts',()=>{throw Error('Demo must not call a financial API')});
 const order=DEMO_ORDERS[0];assert.equal(changeDemoOrder(order,{kind:'refund'}),order);
 const escalated=changeDemoOrder(order,{kind:'escalate',line:0});assert.equal(escalated.refunded,0);
 const pending=changeDemoOrder(escalated,{kind:'refund',amountMinor:2800});assert.equal(pending.refunded,0);assert.equal(pending.case.status,'REFUND_PENDING');assert.equal(changeDemoOrder(pending,{kind:'refund'}),pending);const refunded=changeDemoOrder(pending,{kind:'settle'});assert.equal(refunded.refunded,2800);assert.equal(refunded.fee,2500);
 assert.equal(changeDemoOrder(refunded,{kind:'refund'}),refunded);
 assert.equal(changeDemoOrder(order,{kind:'escalate',line:99}),order);
});


test('Demo partial approval and shared amount validation reject invalid money',()=>{
 const {DEMO_ORDERS,changeDemoOrder}=load('lib/simulated-orders.ts',()=>{throw Error('No financial API in demo')});
 const {refundAmountMinor}=load('components/refund-approval-panel.tsx',()=>{});
 for(const input of ['0','-1','2.001','1e2','Infinity','29'])assert.equal(refundAmountMinor(input,2800),null);
 assert.equal(refundAmountMinor('12.50',2800),1250);
 const order=changeDemoOrder(DEMO_ORDERS[0],{kind:'escalate',line:0});
 assert.equal(changeDemoOrder(order,{kind:'refund',amountMinor:2801}),order);
 const pending=changeDemoOrder(order,{kind:'refund',amountMinor:1250});
 assert.equal(pending.refunded,0);assert.equal(changeDemoOrder(pending,{kind:'settle'}).refunded,1250);
});

test('Payment acknowledgement refuses unverified and browser-only capture',()=>{
 const {acknowledgementText}=load('components/payment-acknowledgement.tsx',()=>{});
 for(const kind of [null,'BROWSER_CALLBACK'])assert.throws(()=>acknowledgementText({order:{payment:{capture_evidence:kind?{kind}:null}}}),/Verified capture/);
});
test('Payment acknowledgement labels test money and uses recorded identifiers',()=>{
 const {acknowledgementText}=load('components/payment-acknowledgement.tsx',()=>{});
 const value=acknowledgementText({mode:'test',provider:'Razorpay',method:'netbanking',provider_status:'captured',order:{reference:'RS-test',order_id:'owned-order',amount_minor:5750,currency:'INR',amount:{display:'₹57.50'},created_at:'2026-09-10T00:00:00Z',refunds:[],payment:{state:'CAPTURED',razorpay_payment_id:'pay_recorded',razorpay_order_id:'order_recorded',capture_evidence:{kind:'WEBHOOK',verified_at:'2026-09-10T00:00:00Z'}}}});
 assert.match(value,/Demo billed to: Vedant Tyagi/);assert.match(value,/TEST MODE — NO REAL MONEY/);assert.match(value,/pay_recorded/);assert.match(value,/₹57.50/);assert.match(value,/Not a Razorpay-issued document or tax invoice/);
});

test('Spoken payment choices handle English, Hinglish and Hindi without accepting negation',()=>{
 const {checkoutChoice}=load('lib/checkout-choice.ts',()=>{});
 for(const text of ['Pay with Razorpay','Razorpay se pay karo','रेज़रपे से भुगतान करो','रेजरपे से पेमेंट करो'])assert.equal(checkoutChoice(text),'manual',text);
 for(const text of ['Pay with Reserve Pay','Reserve Pay se pay karo','रिज़र्व पे से भुगतान करो','रिजर्व पे से भुगतान करो'])assert.equal(checkoutChoice(text),'reserve',text);
 for(const text of ['Do not pay with Razorpay','Reserve Pay se mat karo','रिज़र्व पे से भुगतान नहीं करना','Razorpay or Reserve Pay'])assert.equal(checkoutChoice(text),'clarify',text);
});

test('Fresh review requires backend cancellation permission and no unresolved payment',()=>{
 const {canRefreshCheckout}=load('lib/checkout-recovery.ts',()=>{});
 const base={order_id:null,cancellable:true,attempt:{attempt_id:'same-attempt'},state:'PAYMENT_FAILED'};
 assert.equal(canRefreshCheckout(base),true);
 for(const state of ['AWAITING_PAYMENT','PAYMENT_UNKNOWN','RECONCILING','PAID']) assert.equal(canRefreshCheckout({...base,state}),false);
 assert.equal(canRefreshCheckout({...base,cancellable:false}),false);
 assert.equal(canRefreshCheckout({...base,order_id:'confirmed-order'}),false);
 assert.equal(canRefreshCheckout({...base,attempt:null,state:'APPROVAL_REQUIRED'}),true);
});
test('Cancelling a resolved checkout clears only its own payment recovery data',()=>{
 const records=new Map([['rs-manual-pending',JSON.stringify({checkoutId:'other-checkout'})],['rs-reserve-pending',JSON.stringify({card:{checkout_id:'cancelled-checkout'}})]]);
 const api=load('lib/checkout-recovery.ts',()=>{},{sessionStorage:{getItem:k=>records.get(k),removeItem:k=>records.delete(k)}});
 api.clearCancelledCheckoutRecovery('cancelled-checkout');
 assert.equal(records.has('rs-reserve-pending'),false);assert.equal(records.has('rs-manual-pending'),true);
});

test('Payment polling releases abort listeners after each wait and stops on abort',async()=>{
 const listeners=new Set();let aborted=false,reads=0,cancelled=0;
 const signal={get aborted(){return aborted},addEventListener:(_name,fn)=>listeners.add(fn),removeEventListener:(_name,fn)=>listeners.delete(fn)};
 let timer;
 const commerce={checkout:{read:async()=>{reads++;return {state:'PAYMENT_UNKNOWN'}}}};
 const api=load('lib/manual-pay.ts',()=>{},{DOMException,require:()=>({commerce}),setTimeout:fn=>{timer=fn;return 1},clearTimeout:()=>cancelled++});
 const polling=api.awaitSettlement('same-checkout',{signal});
 await new Promise(resolve=>setImmediate(resolve));
 for(let i=0;i<20;i++){
  assert.equal(listeners.size,1);timer();assert.equal(listeners.size,0);
  await new Promise(resolve=>setImmediate(resolve));
 }
 const before=reads;aborted=true;for(const listener of listeners)listener();
 await assert.rejects(polling,{name:'AbortError'});
 assert.equal(listeners.size,0);assert.equal(cancelled,1);assert.equal(reads,before);
});

test('Reserve failure retains unresolved recovery and announces only changed allocation',async()=>{
 const card={checkout_id:'checkout',content_hash:'hash',amount_minor:100,quote:{lines:[],items_subtotal_minor:100,items_tax_minor:0,delivery_tax_minor:0,delivery_fee_minor:0,discount_minor:0}};
 const authority={authority_id:'authority',available_minor:1000,capacity_minor:1000,per_purchase_limit_minor:200,allowed_skus:null};
 const pending={card,authority,key:'stable-key',attemptId:'attempt'};
 let saved=JSON.stringify(pending),stateIndex=0,allocation='ALLOCATED',poll;
 const messages=[];const cleanups=[];
 const initial=[card,authority,[],null,'',false,false,pending];
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:()=>[initial[stateIndex++],()=>{}],useRef:value=>({current:value}),useEffect:fn=>{const cleanup=fn();if(cleanup)cleanups.push(cleanup)}};
 const jsx={jsx:()=>null,jsxs:()=>null};
 const api=load('components/reserve-checkout.tsx',()=>{},{setInterval:fn=>{poll=fn;return 1},clearInterval:()=>{},sessionStorage:{getItem:()=>saved,setItem:(_k,v)=>saved=v,removeItem:()=>saved=null},require:name=>name==='react'?hooks:name==='react/jsx-runtime'?jsx:name.endsWith('reserve-api')?{commerce:async()=>({status:'FAILED',allocation,attempt_id:'attempt'}),permissions:async()=>({authorities:[authority]})}:name.endsWith('demo')?{money:x=>String(x)}:{}});
 api.ReserveCheckout({total:100,lines:[],basket:{},reviewed:card,onGuidance:text=>messages.push(text),onConfirmed:()=>assert.fail('Failed debit must not confirm an order'),onBack:()=>{},onFreshReview:async()=>{}});
 await new Promise(resolve=>setImmediate(resolve));
 const count=messages.length;assert.ok(saved,'Unknown capacity must preserve the retry key');
 await poll();assert.equal(messages.length,count);assert.ok(saved);
 allocation='RELEASED';await poll();assert.equal(saved,null);assert.equal(messages.length,count+1);
 await poll();assert.equal(messages.length,count+1);
 for(const cleanup of cleanups)cleanup();
});

test('Payment discussion cannot authorize Reserve and UPI is not UAP',()=>{
 const {checkoutChoice}=load('lib/checkout-choice.ts',()=>{});
 for(const text of ['Is Reserve Pay safe','Can I use Reserve Pay','Tell me my Reserve Pay balance','Use Reserve Pay if the bill is below 200','Should I use Razorpay','Wait, Reserve Pay','रिजर्व पे का बैलेंस बताओ','रिजर्व पे कैसे काम करता है','रेजरपे से अभी नही देना','AI से पेमेंट होगा?']) assert.equal(checkoutChoice(text),'clarify',text);
 for(const text of ['Pay with UPI','यूपीआई से भुगतान करो','यू पी आई से भुगतान करो']) assert.equal(checkoutChoice(text),'manual',text);
 for(const text of ['Pay with UAP','यू ए पी से भुगतान करो','एआई से पेमेंट करो']) assert.equal(checkoutChoice(text),'reserve',text);
});

test('Naming a payment service in ordinary conversation is not a payment instruction',()=>{
 const {checkoutChoice}=load('lib/checkout-choice.ts',()=>{});
 for(const text of ['I like Reserve Pay','Reserve Pay sounds interesting','Razorpay is a company','AI is helpful','मुझे रिजर्व पे पसंद है'])assert.equal(checkoutChoice(text),'clarify',text);
 for(const text of ['Reserve Pay','Reserve Pay please','please Razorpay','UPI','रिजर्व पे','रिजर्व पे से करो'])assert.notEqual(checkoutChoice(text),'clarify',text);
});

test('Catalogue, Reserve and voice share first-session initialization before concurrent reads',async()=>{
 let ready;const bootstrap=new Promise(resolve=>ready=resolve);const calls=[];
 const fetch=async url=>{calls.push(url);if(calls.length===1)return bootstrap;return reply(url.includes('ticket')?{ticket:'fixture'}:{authorities:[],products:[],checkouts:[]})};
 const api=load('lib/commerce.ts',fetch,{window:{}});
 const reserve=load('lib/reserve-api.ts',fetch,{window:{},require:()=>api});
 const voice=load('lib/voice/client.ts',fetch,{window:{},require:()=>({...api,Microphone:class{}})});
 const requests=[api.commerce.catalogue.list({limit:1}),reserve.permissions(),voice.VoiceClient.ticket()];
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(calls,['/api/commerce/carts/current']);
 ready(reply({cart:null}));await Promise.all(requests);
 assert.equal(calls.filter(x=>x==='/api/commerce/carts/current').length,1);
 assert.ok(calls.includes('/api/commerce/reserve/authorities'));assert.ok(calls.includes('/api/voice/ticket'));
});

test('Only explicit checkout navigation opens bill review across languages',()=>{
 const {requestsCheckoutReview}=load('lib/checkout-choice.ts',()=>{});
 for(const text of ['Checkout','Please show my bill','Take me to checkout','Review my order','Mera bill dikhao','बिल दिखाओ','मेरा बिल दिखाइए','चेकआउट करो'])assert.equal(requestsCheckoutReview(text),true,text);
 for(const text of ['Do not checkout','How does checkout work?','Add milk then checkout','Show me milk','मुझे बिल नहीं दिखाओ','Pay with Reserve Pay','Track my order'])assert.equal(requestsCheckoutReview(text),false,text);
});

for(const admitted of [false,true])test(`Edited cart rebuilds an invalidated bill only without an admitted attempt (${admitted})`,async()=>{
 const states=[];let created=0;
 const cart={cart_id:'same-cart',lines:[{sku:'milk',quantity:2}],unavailable:[]};
 const view={checkout_id:'retired',cart_id:cart.cart_id,state:'INVALIDATED',order_id:null,attempt:admitted?{attempt_id:'existing-payment'}:null};
 const commerce={checkout:{list:async()=>({checkouts:[view]}),read:async()=>view},cart:{current:async()=>({cart}),read:async()=>cart,checkout:async()=>{created++;return {checkout_id:'new-reviewed-bill'}}}};
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:initial=>[initial,value=>states.push(value)],useRef:value=>({current:value}),useCallback:fn=>fn,useEffect:fn=>fn()};
 const api=load('lib/authoritative-bill.ts',()=>{},{require:name=>name==='react'?hooks:{commerce,CommerceError:Error,idempotencyKey:()=>crypto.randomUUID()}});
 api.useAuthoritativeBill(cart.lines,true,cart.cart_id);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(created,admitted?0:1);assert.equal(states.at(-1).status,admitted?'recovering':'ready');
});


test('Reserve cannot construct a second purchase when the reviewed bill is missing',async()=>{
 const states=[];let creates=0;
 const authority={authority_id:'authority',status:'ACTIVE',authorization_evidence:{status:'VERIFIED'}};
 const hooks={useLayoutEffect:fn=>fn(),useEffectEvent:fn=>fn,useState:v=>[v,value=>states.push(value)],useRef:value=>({current:value}),useEffect:fn=>fn()};
 const api=load('components/reserve-checkout.tsx',()=>{},{sessionStorage:{getItem:()=>null},require:name=>name==='react'?hooks:name==='react/jsx-runtime'?{jsx:()=>null,jsxs:()=>null}:name.endsWith('reserve-api')?{permissions:async()=>({authorities:[authority]}),commerce:async()=>{creates++;throw Error('Unexpected mutation')}}:name.endsWith('demo')?{money:String}:{}});
 api.ReserveCheckout({total:100,lines:[],basket:{},reviewed:null,onGuidance:()=>{},onConfirmed:()=>assert.fail('No bill'),onBack:()=>{},onFreshReview:async()=>{}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(creates,0);assert.ok(states.some(v=>typeof v==='string'&&v.includes('reviewed bill is unavailable')));
});


test('Settlement exits an invalidated checkout with a verified failed attempt',async()=>{
 const commerce={checkout:{read:async()=>({state:'INVALIDATED',order_id:null,attempt:{state:'FAILED'}})}};
 const api=load('lib/manual-pay.ts',()=>{},{require:()=>({commerce}),setTimeout:()=>assert.fail('Must not loop after a verified failure')});
 assert.equal((await api.awaitSettlement('retired-checkout')).kind,'failed');
});

test('server payment deadline closes the existing provider UI without claiming failure',async()=>{
 let options,closes=0,clock=0;const timers=new Map();let seq=0;
 class Checkout{constructor(o){options=o}on(){}open(){}close(){closes++;options.modal.ondismiss()}}
 const api=load('lib/razorpay.ts',()=>{throw Error('No second payment')},{window:{Razorpay:Checkout},performance:{now:()=>clock},document:{body:{style:{removeProperty(){}},appendChild(){}},querySelectorAll:()=>[],createElement:()=>({style:{},dataset:{},remove(){}})},setInterval:()=>99,clearInterval:()=>{},setTimeout:(fn,ms)=>{timers.set(++seq,{fn,ms});return seq},clearTimeout:id=>timers.delete(id)});
 const h={keyId:'rzp_test_fixture',orderId:'deadline_order',amountMinor:5750,currency:'INR',merchantName:'Test',description:'Test',remainingMs:2000};
 const result=api.openRazorpay(h);await Promise.resolve();
 const expiry=[...timers.values()].find(t=>t.ms===2000);assert.ok(expiry);
 clock=2000;expiry.fn();assert.equal((await result).kind,'dismissed');assert.equal(closes,1);
 await assert.rejects(api.openRazorpay({...h,remainingMs:0}),/Payment window closed/);
 assert.equal(closes,1);
});

test('Blank-frame reload targets only the existing checkout without opening another payment',async()=>{
 let options,timer,navigated,opens=0;const buttons=[];
 class Checkout {constructor(value){options=value}on(){}open(){opens++}close(){options.modal.ondismiss()}}
 const doc={querySelectorAll:()=>[],createElement:()=>({setAttribute(){},style:{},remove(){}}),body:{appendChild:b=>buttons.push(b)}};
 const api=load('lib/razorpay.ts',()=>{throw Error('Recovery must not create a payment')},{window:{Razorpay:Checkout,location:{origin:'http://localhost:3000',assign:href=>{navigated=href}}},document:doc,setTimeout:fn=>{timer=fn;return 1},clearTimeout:()=>{}});
 const id='01a08d73-9114-7e36-9c62-c1853f4db476';
 const pending=api.openRazorpay({checkoutId:id,keyId:'rzp_test_fixture',orderId:'same-order',amountMinor:5750,currency:'INR',merchantName:'Test',description:'Test'});
 await Promise.resolve();timer();
 buttons.find(b=>b.textContent.startsWith('Blank screen?')).onclick();
 assert.equal(navigated,`http://localhost:3000/shop?recover_checkout=${id}`);assert.equal(opens,1);
 assert.equal(options.order_id,'same-order');
 buttons.find(b=>b.textContent==='Return to payment status').onclick();
 assert.equal((await pending).kind,'dismissed');
});
