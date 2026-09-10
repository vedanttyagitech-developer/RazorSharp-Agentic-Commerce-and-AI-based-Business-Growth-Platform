'use client';
import Image from 'next/image';

import type {ReviewCard} from '@/lib/reserve-api';
import {skuOf} from '@/lib/reserve-api';
import {useAuthoritativeBill} from '@/lib/authoritative-bill';
import {totalMinor} from '@/lib/commerce';
import {canChoosePayment} from '@/lib/checkout-choice';
import {useState} from 'react';
import {ReserveCheckout,type ReserveConfirmation} from './reserve-checkout';
import {ManualCheckout,type ManualConfirmation} from './manual-checkout';
import {CheckoutRecovery} from './checkout-recovery';
import {PaymentAcknowledgement} from './payment-acknowledgement';
import {KernelTiming} from './transaction-kernel';
import {useOrder} from '@/lib/order-timing';
import {CheckoutAssistant,PaymentMethods} from './checkout-payment';
import { ArrowRight, Check, Clock3, LockKeyhole, MapPin, Minus, Plus, ShieldCheck, ShoppingBag } from 'lucide-react';
import { Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription } from '@/components/ui/dialog';
import { type Product,money } from '@/lib/demo';
import { VoiceWave } from '@/components/voice-wave';

type ReviewProps={onNavigate:(text:string)=>boolean;onRecoveredOrder:(order:import('@/lib/commerce').Order)=>void;onFreshReview:(id:string)=>Promise<void>;cartId?:string;cartBusy:boolean;onContinue:(text:string)=>void;open:boolean;onClose:()=>void;lines:Product[];basket:Record<string,number>;subtotal:number;discount:number;delivery:number;total:number;stage:string;onQuantity:(id:string,delta:number,source?:HTMLElement)=>void;onManualConfirmed:(result:ManualConfirmation)=>void;onReserveConfirmed:(result:ReserveConfirmation)=>void;onOrder:()=>void;onProof:()=>void;onPaymentLock:(locked:boolean)=>void};

export function OrderReview({onNavigate,onRecoveredOrder,onFreshReview,cartId,cartBusy,onContinue,open,onClose,lines,basket,subtotal,discount,delivery,total,stage,onQuantity,onManualConfirmed,onReserveConfirmed,onOrder,onProof,onPaymentLock}:ReviewProps){
 const count=lines.reduce((sum,p)=>sum+basket[p.id],0);
 const confirming=stage==='checking',confirmed=stage==='confirmed';
 const [handoff,setHandoff]=useState<'choose'|'manual'|'reserve'>('choose');
 const [recoveryGuidance,setRecoveryGuidance]=useState('Checking your existing checkout. Please do not pay again until its status is verified.');
 const [manualGuidance,setManualGuidance]=useState('Razorpay Checkout will open for this exact bill.');
 const [reserveBill,setReserveBill]=useState<ReviewCard|null>(null);const [reserveCommit,setReserveCommit]=useState(0);
 const [reserveGuidance,setReserveGuidance]=useState('Checking your saved Reserve Pay permission.');
 // The order this review produced, kept so the confirmed screen can name it and read its
 // timing. The parent is told too, but it renders a different surface; a screen that has
 // just confirmed a purchase should not have to ask another one what it confirmed.
 const [placed,setPlaced]=useState<{orderId:string;reference:string|null;remainingMinor?:number|null}|null>(null);
 const placedOrder=useOrder(placed?.orderId??null);
 // The bill the buyer is shown, and the one both payment paths bind to. Built from the
 // backend as soon as the review opens, BEFORE a payment method can be chosen -- the
 // screen used to render local arithmetic and let Reserve discover a different backend
 // total after the buyer had already committed to a method.
 const request=lines.map(p=>({sku:skuOf(p),quantity:basket[p.id]})).filter(l=>l.quantity>0);
 const bill=useAuthoritativeBill(request,open&&!confirming&&!confirmed&&!cartBusy,cartId);
 const authoritative=bill.state.status==='ready'?bill.state.card:null;
 // Display figures come from the card's own quote. Nothing here adds anything up.
 const [lastCheckout,setLastCheckout]=useState<string|undefined>();if(authoritative&&authoritative.checkout_id!==lastCheckout)setLastCheckout(authoritative.checkout_id);
 const q=authoritative?.quote;
 const showSubtotal=q?q.items_subtotal_minor:subtotal;
 const showDelivery=q?q.delivery_fee_minor:delivery;
 const showDiscount=q?q.discount_minor:discount;
 const showTotal=authoritative?totalMinor(authoritative.quote):total;
 const priced=!!authoritative;
 const canChoose=()=>!cartBusy&&priced&&canChoosePayment(count,stage,handoff);
 const chooseManual=()=>{if(!canChoose())return;onPaymentLock(true);setHandoff('manual')};
 const chooseReserve=()=>{if(handoff==='reserve'&&reserveBill){setReserveCommit(n=>n+1);return}if(!canChoose())return;onPaymentLock(true);setHandoff('reserve')};
 const [previousStage,setPreviousStage]=useState(stage);if(previousStage!==stage){setPreviousStage(stage);if(stage==='basket')setHandoff('choose')}

 return <Dialog open={open} onOpenChange={v=>{if(v)return;onClose()}}><DialogContent className="checkout-shell"><div style={{viewTransitionName:'order-surface'}} className={`order-review-dialog checkout-card ${confirmed?'is-confirmed':''}`}>
  <DialogHeader className="review-heading"><div className="review-eyebrow"><span><ShoppingBag size={15}/> GREEN BASKET</span><span className="review-demo signature-review"> RAZORSHARP · DEMO</span></div><DialogTitle>{confirmed?'A good choice. All yours.':confirming?'A moment for the important part.':'One last look.'}</DialogTitle><DialogDescription>{confirmed?'Your sample order is confirmed. No real payment was made.':confirming?'Your approval is recorded in this preview. We’re waiting for simulated payment confirmation.':'Your finds, the full price, and the final say. All in one place.'}</DialogDescription></DialogHeader>
  {bill.state.status==='recovering'&&!confirmed?<CheckoutRecovery onGuidance={setRecoveryGuidance} onConfirmed={order=>{setPlaced(previous=>({...previous,orderId:order.order_id,reference:order.reference}));onRecoveredOrder(order)}} onFreshReview={onFreshReview} initial={bill.state.view} onOrder={onOrder}/>:!confirming&&!confirmed&&handoff==='reserve'?<ReserveCheckout onFreshReview={onFreshReview} reviewed={authoritative as unknown as ReviewCard|null} commitRequest={reserveCommit} onReviewState={setReserveBill} total={showTotal} onGuidance={setReserveGuidance} onConfirmed={r=>{setPlaced({orderId:r.orderId,reference:null,remainingMinor:r.remainingMinor});onReserveConfirmed(r)}} onBack={()=>{onPaymentLock(false);setHandoff('choose')}}/>:!confirming&&!confirmed&&handoff==='manual'&&authoritative?<ManualCheckout onReviewChanged={()=>{bill.retry();onPaymentLock(false);setHandoff('choose')}} onFreshReview={onFreshReview} card={authoritative} onGuidance={setManualGuidance} onConfirmed={r=>{setPlaced({orderId:r.orderId,reference:r.reference});onManualConfirmed(r)}} onBack={()=>{onPaymentLock(false);setHandoff('choose')}}/>:confirming||confirmed?<div className="review-outcome">
   <div className={`review-result-symbol ${confirmed?'done':''}`}>{confirmed?<Check size={42}/>:<LockKeyhole size={35}/>}<i/><i/></div>
   <div className="review-outcome-amount">{money(total)}<span>{confirmed?'Confirmed in this demo':'Awaiting simulated confirmation'}</span></div>
   {confirming?<><VoiceWave mode="thinking" className="review-wait-wave"/><div className="review-confirmation-steps"><span className="done"><Check size={14}/> Your exact order approved</span><span><Clock3 size={14}/> Checking simulated payment evidence</span></div><p>There’s no need to try paying again.</p></>:<><div className="review-product-keepsake">{lines.slice(0,4).map((p,i)=><div key={p.id} style={{'--item-index':i} as React.CSSProperties}><Image src={p.image} alt={p.name} unoptimized width={640} height={640}/></div>)}</div><div className="review-confirmed-reference"><span>{placedOrder.status==='ready'?`Order ${placedOrder.order.reference}`:placed?.reference?`Order ${placed.reference}`:placed?`Order ${placed.orderId}`:'Reading order reference…'}</span><strong>{count} items · Green Basket</strong></div>
   {placed?.remainingMinor!==undefined&&<p className="review-price-note">Reserve balance remaining: <strong>{placed.remainingMinor===null?'Could not refresh — check Reserve Pay':money(placed.remainingMinor)}</strong></p>}
   {placedOrder.status==='ready'&&<KernelTiming order={placedOrder.order}/>}
   {confirmed&&placed?.orderId&&<PaymentAcknowledgement orderId={placed.orderId}/>}
   {placedOrder.status==='loading'&&<output className="review-price-note">Reading how long this transaction took…</output>}
   {placedOrder.status==='unavailable'&&<p className="review-price-note">Your order is confirmed. Its timing could not be read: {placedOrder.detail}</p>}<button className="primary" onClick={onOrder}>Follow your order <ArrowRight size={17}/></button><button className="subtle" onClick={onProof}><ShieldCheck size={15}/> Explore the purchase proof</button></>}
  </div>:<div className="review-layout"><section className="review-selection">
   <div className="review-selection-title"><h3>A little everyday goodness.</h3><span>{count} {count===1?'item':'items'}</span></div>
   <div className="review-items">{lines.map((p,i)=><div className="review-item" key={p.id} style={{'--item-index':i} as React.CSSProperties}><div className="review-item-image"><Image src={p.image} alt={p.name} unoptimized width={640} height={640}/></div><div className="review-item-details"><strong>{p.name}</strong><span>{p.unit}</span><div className="review-quantity"><button aria-label={`Remove one ${p.name} from reviewed order`} onClick={e=>onQuantity(p.id,-1,e.currentTarget.closest('.review-item') as HTMLElement)}><Minus size={13}/></button><span key={basket[p.id]}>{basket[p.id]}</span><button aria-label={`Add one ${p.name} to reviewed order`} onClick={e=>onQuantity(p.id,1,e.currentTarget.closest('.review-item') as HTMLElement)}><Plus size={13}/></button></div></div><strong className="review-item-price">{q?money(q.lines.find(l=>l.sku===skuOf(p))?.subtotal_minor??0):'—'}</strong></div>)}</div>
   {!count&&<div className="review-empty"><ShoppingBag size={26}/><p>Your cart is empty. Add a few finds to continue.</p><button className="secondary" onClick={onClose}>Back to your copilot</button></div>}
   <div className="review-delivery"><span className="review-location-icon"><MapPin size={20}/></span><div><strong>Your everyday, delivered.</strong><p>Bengaluru · sample delivery location</p><span><Clock3 size={13}/> Delivery timing confirmed by the store</span></div></div>
  </section><aside className="review-summary">
   <span className="review-total-label">YOUR ORDER TOTAL</span><div className="review-total" key={showTotal}>{priced?money(showTotal):'—'}</div><p className="review-price-note">{priced?'Priced by the store. Everything included.':'Asking the store for the exact price…'}</p>
   <div className="review-breakdown"><div><span>{count} items</span><strong>{priced?money(showSubtotal):'—'}</strong></div><div><span>Delivery</span><strong>{priced?(showDelivery?money(showDelivery):'On us'):'—'}</strong></div>{priced&&showDiscount>0&&<div className="review-savings"><span>{q?.offer_label||'Merchant offer'}</span><strong>−{money(showDiscount)}</strong></div>}<div><span>Tax</span><strong>{q?money(q.items_tax_minor+q.delivery_tax_minor):'—'}</strong></div><div className="review-final"><span>Total payable</span><strong>{priced?money(showTotal):'—'}</strong></div></div>
   {bill.state.status==='building'&&<output className="review-price-note">Building your order with the store — {bill.state.step==='cart'?'opening a cart':bill.state.step==='lines'?'adding your items':'checking prices and availability'}.</output>}
   {bill.state.status==='unavailable'&&<div className="review-changed" role="alert"><strong>{bill.state.error.title}</strong><p>{bill.state.error.detail}</p>{bill.state.retryable&&<button className="secondary" onClick={bill.retry}>Try again</button>}</div>}
   {priced&&<p className="review-stock-note">Items aren’t reserved yet. We’ll check availability again when you pay.</p>}
   <div className="review-assurance"><ShieldCheck size={20}/><div><strong>Your yes belongs to this exact order.</strong><p>Changes to items, fees or offers need a fresh approval. Your original sale terms stay with your purchase.</p></div></div>
   <PaymentMethods total={showTotal} disabled={cartBusy||!count||!priced} onManual={chooseManual} onReserve={chooseReserve}/><p className="review-payment-note">{priced?'Razorpay Checkout opens for real on this exact bill. Reserve Pay executes through the backend simulator.':'A payment method can be chosen once the store has priced this order.'}</p>
   <details className="review-terms"><summary>View purchase terms</summary><p>Policy-at-Sale Receipt hash: {authoritative?.policy_receipt_hash??'Awaiting the store’s bill'}. Approval is bound to this checkout’s content and version.</p></details>
  </aside></div>}
 </div>{open&&<CheckoutAssistant onNavigate={onNavigate} version={authoritative?.version??reserveBill?.version} checkoutId={authoritative?.checkout_id??(bill.state.status==='recovering'?bill.state.view.checkout_id:lastCheckout)} onNext={onContinue} onManual={chooseManual} onReserve={chooseReserve} checkoutStage={confirmed?'success':bill.state.status==='recovering'?'verifying':confirming?'verifying':handoff==='manual'?'manual':handoff==='reserve'?(reserveBill?'reserve-review':'verifying'):!priced?'verifying':'review'} total={reserveBill?.amount_minor??showTotal} subtotal={reserveBill?.quote.items_subtotal_minor??showSubtotal} tax={(reserveBill?.quote.items_tax_minor??q?.items_tax_minor??0)+(reserveBill?.quote.delivery_tax_minor??q?.delivery_tax_minor??0)} delivery={reserveBill?.quote.delivery_fee_minor??showDelivery} discount={reserveBill?.quote.discount_minor??showDiscount} message={confirmed?'Your payment is confirmed and your order is placed. What would you like to do next?':bill.state.status==='recovering'?recoveryGuidance:handoff==='manual'&&!confirming&&!confirmed?manualGuidance:handoff==='reserve'&&!confirming&&!confirmed?reserveGuidance:confirmed?'Your test payment is confirmed, and your demo order has been placed. What would you like to do next?':confirming?'Checking the simulated payment and preparing your demo order. Please do not pay again.':!priced?'Please wait while the store checks your bill. Choose a payment method after the exact amount is available.':`How would you like to pay ${money(showTotal)}: manually through Razorpay checkout, or with AI-assisted Reserve Pay? Tell me your choice, or use a button. Reserve Pay uses a simulated provider here.`}/>}
 </DialogContent></Dialog>
}
