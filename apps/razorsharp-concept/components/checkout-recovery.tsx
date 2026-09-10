'use client';
import {useEffect,useRef,useState} from 'react';
import {commerce,rawCommerceCall,CommerceError,type CheckoutView} from '@/lib/commerce';
import {openRazorpay} from '@/lib/razorpay';
import {canRefreshCheckout} from '@/lib/checkout-recovery';
import {PaymentAcknowledgement} from './payment-acknowledgement';

/** Recover the server's purchase; never record a new approval during payment recovery. */
export function CheckoutRecovery({initial,onOrder,onFreshReview,onConfirmed}:{initial:CheckoutView;onOrder:()=>void;onFreshReview:(id:string)=>Promise<void>;onConfirmed:(order:import('@/lib/commerce').Order)=>void}) {
 const [view,setView]=useState(initial),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [method,setMethod]=useState<'manual'|'reserve'|null>(null);
 const verifyKey=useRef(crypto.randomUUID());
 const confirmedCallback=useRef(onConfirmed);confirmedCallback.current=onConfirmed;
 useEffect(()=>{if(!view.order_id)return;let cancelled=false;void commerce.orders.read(view.order_id).then(order=>{if(!cancelled)confirmedCallback.current(order)}).catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}},[view.order_id]);
 useEffect(()=>{let cancelled=false;let timer:ReturnType<typeof setTimeout>;
  const poll=async()=>{try{const next=await commerce.checkout.read(initial.checkout_id);if(!cancelled){setView(next);setError('')}}catch(e){if(!cancelled)setError((e as Error).message)}finally{if(!cancelled)timer=setTimeout(poll,2000)}};
  void poll();return()=>{cancelled=true;clearTimeout(timer)};
 },[initial.checkout_id]);
 useEffect(()=>{let cancelled=false;const attempt=view.attempt?.attempt_id;if(!attempt)return;
  void rawCommerceCall(`reserve/payments/${attempt}`).then(()=>{if(!cancelled)setMethod('reserve')}).catch(e=>{if(cancelled)return;if(e instanceof CommerceError&&e.status===404)setMethod('manual');else setError(e.message)});
  return()=>{cancelled=true};
 },[view.attempt?.attempt_id]);
 useEffect(()=>{if(method!=='manual'||view.order_id)return;void commerce.payments.reconcile(view.checkout_id).catch(e=>setError(e.message))},[method,view.checkout_id,view.order_id]);
 async function resume(){if(busy)return;setBusy(true);setError('');try{
  const current=await commerce.checkout.read(view.checkout_id);setView(current);
  if(current.order_id||current.state!=='AWAITING_PAYMENT')return;
  const h=await commerce.checkout.payment(view.checkout_id);
  if(!h.razorpay_order_id||!h.razorpay_key_id||h.attempt_id!==current.attempt?.attempt_id)throw Error('The existing provider order is not ready. Wait for its status.');
  const result=await openRazorpay({keyId:h.razorpay_key_id,orderId:h.razorpay_order_id,amountMinor:h.amount_minor,currency:h.currency,merchantName:h.merchant_name??'Merchant',description:h.description??'Existing checkout'});
  if(result.kind==='reported')await commerce.payments.verify({checkout_id:view.checkout_id,...result.report},verifyKey.current);
  else await commerce.payments.reconcile(view.checkout_id);
 }catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 return <section className="review-outcome" aria-label="Recovering checkout">
  <h2>{view.order_id?'Your order is confirmed':view.state==='PAYMENT_FAILED'?'Payment did not go through':'Checking your payment'}</h2>
  {view.order_id&&<PaymentAcknowledgement orderId={view.order_id}/>}
  <p role="status">{view.order_id?`Order ${view.order_reference??view.order_id}`:`Status: ${view.state}. We are checking this purchase before allowing another payment.`}</p>
  {canRefreshCheckout(view)&&<button className="secondary" onClick={()=>void onFreshReview(view.checkout_id).catch(e=>setError(e.message))}>Refresh stock and review again</button>}
  {error&&<p role="alert">{error}. No payment outcome has been inferred.</p>}
  {view.order_id?<button className="primary" onClick={onOrder}>View orders</button>:method==='manual'&&view.state==='AWAITING_PAYMENT'&&view.attempt?.razorpay_order_id?<button className="secondary" disabled={busy} onClick={()=>void resume()}>Resume this Razorpay checkout</button>:view.state==='PAYMENT_FAILED'?<p>The backend confirmed failure. Review current stock and prices before trying again.</p>:<p>Keep this view open for the backend outcome. Refreshing does not create another purchase.</p>}
 </section>;
}
