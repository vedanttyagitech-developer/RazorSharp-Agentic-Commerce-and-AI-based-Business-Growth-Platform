'use client';
import {Check,LockKeyhole,ShieldCheck,TriangleAlert,WalletCards,CreditCard} from 'lucide-react';
import {money} from '@/lib/demo';
import './payment-animation.css';
import './reserve-transfer.css';
import {ReservePaymentFrame} from './reserve-payment-frame';
import {ReserveConfirmedItems,type ReserveConfirmedOrder} from './reserve-confirmed-items';
import {PaymentAcknowledgement} from './payment-acknowledgement';

export type PaymentAnimationState='processing'|'verifying'|'attention'|'failed'|'completed';
/** Neither an animation nor a browser return can establish a completed order. */
export function paymentAnimationState(state:string,orderId?:string|null,interrupted=false):PaymentAnimationState{
 if(['CAPTURED','PAID','CONFIRMED'].includes(state)&&orderId)return 'completed';
 if(interrupted||['UNKNOWN','ESCALATED','RECONCILING'].includes(state))return 'attention';
 if(['FAILED','PAYMENT_FAILED','EXPIRED','CANCELLED','INVALIDATED'].includes(state))return 'failed';
 if(['CAPTURED','PAID','CONFIRMED','VERIFYING'].includes(state))return 'verifying';
 return 'processing';
}
export function PaymentAnimation({state,orderId,amount,method='reserve',interrupted=false,caption,admitted=false,confirmedOrder}:{state:string;orderId?:string|null;amount?:number;method?:'reserve'|'razorpay';interrupted?:boolean;caption?:string;admitted?:boolean;confirmedOrder?:ReserveConfirmedOrder}){
 const phase=paymentAnimationState(state,orderId,interrupted),done=phase==='completed';
 const title=done?'Payment completed':phase==='attention'?'Your payment needs a check':phase==='failed'?'Payment not completed':phase==='verifying'?'Verifying your payment':'Your payment is in motion';
 const detail=done?'Confirmed by the backend. Your order is ready.':phase==='attention'?'The outcome is not confirmed. Keep this payment reference and do not pay again.':phase==='failed'?'No order was confirmed for this payment. Follow the recovery options below.':phase==='verifying'?'Waiting for recorded provider evidence before confirming your order.':caption??'Following this same payment through execution and provider verification.';
 if(done)return <ReservePaymentFrame title={`${method==='reserve'?'Reserve Pay':'Razorpay'} payment flow`}><section className="reserve-transfer order-success-scene" data-method={method} data-state="completed" aria-label="Order successful"><header className="order-success-heading"><span className="order-success-check" aria-hidden="true"><Check size={27}/></span><div><span className="reserve-transfer-eyebrow">{method==='reserve'?'RESERVE PAY':'RAZORPAY CHECKOUT'}</span><h3>Order successful</h3><p>Payment completed. Your order is confirmed.</p></div></header>{confirmedOrder&&confirmedOrder.orderId===orderId?<div className="order-success-columns"><ReserveConfirmedItems order={confirmedOrder}/><PaymentAcknowledgement orderId={confirmedOrder.orderId} compact/></div>:<output>{amount!==undefined&&<strong>{money(amount)} · </strong>}Reading your confirmed order items… {method==='reserve'?'SIMULATED PROVIDER · No real bank transfer.':''}</output>}</section></ReservePaymentFrame>;
 return <ReservePaymentFrame title={`${method==='reserve'?'Reserve Pay':'Razorpay'} payment flow`}><section className="reserve-transfer payment-centre-scene" data-method={method} data-state={phase} aria-label={`${method==='reserve'?'Reserve Pay':'Razorpay'} payment status`}><header className="reserve-transfer-heading"><div><span className="reserve-transfer-eyebrow">{method==='reserve'?'RESERVE PAY':'RAZORPAY CHECKOUT'}</span><h3>{title}</h3></div>{amount!==undefined&&<strong className="reserve-transfer-amount">{money(amount)}</strong>}</header>
 {done&&confirmedOrder&&confirmedOrder.orderId===orderId?<><ReserveConfirmedItems order={confirmedOrder}/><PaymentAcknowledgement orderId={confirmedOrder.orderId} compact/></>:<><div className="centre-payment-orbit" data-state={phase} aria-hidden="true"><i/><i/><div className="centre-payment-token">{done?<Check size={38}/>:phase==='attention'||phase==='failed'?<TriangleAlert size={38}/>:method==='reserve'?<WalletCards size={42}/>:<CreditCard size={42}/>}</div><span><LockKeyhole size={16}/></span></div>{done&&<output>Reading your confirmed order items…</output>}</>}
 <div className="reserve-transfer-status"><span className="reserve-transfer-status-icon">{done?<Check size={18}/>:<ShieldCheck size={18}/>}</span><output>{detail}</output></div>{!done&&<p className="centre-admission-note">{admitted?'Execution attempt recorded · awaiting provider evidence':'Checking this exact purchase'}</p>}<footer><small>{method==='reserve'?'SIMULATED PROVIDER · No real bank transfer.':'Payment confirmation requires recorded provider evidence.'}</small></footer></section></ReservePaymentFrame>;
}
