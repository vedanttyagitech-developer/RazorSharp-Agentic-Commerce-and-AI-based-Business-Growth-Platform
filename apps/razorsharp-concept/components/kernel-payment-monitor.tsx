'use client';
import {useEffect,useState} from 'react';
import {createPortal} from 'react-dom';
import {Activity,Fingerprint,ShieldCheck} from 'lucide-react';
import {commerce,type CheckoutView} from '@/lib/commerce';
import './kernel-payment-monitor.css';
import {paymentAnimationState} from './payment-animation';
import {PaymentBill,PaymentKernelFlow} from './payment-journey';

/** A read-only view of server evidence. Animation never advances payment state. */
export function KernelPaymentMonitor({checkoutId,providerOpen=false,floatingOnly=false}:{checkoutId:string;providerOpen?:boolean;floatingOnly?:boolean}) {
 const [snapshot,setSnapshot]=useState<{id:string;view:CheckoutView}|null>(null);
 const [offline,setOffline]=useState(false);
 useEffect(()=>{
  let stopped=false;let timer:ReturnType<typeof setTimeout>;
  async function read(){
   try{const view=await commerce.checkout.read(checkoutId);if(!stopped){setSnapshot({id:checkoutId,view});setOffline(false)}}
   catch{if(!stopped)setOffline(true)}
   finally{if(!stopped)timer=setTimeout(read,4000)}
  }
  void read();return()=>{stopped=true;clearTimeout(timer)};
 },[checkoutId]);
 const view=snapshot?.id===checkoutId?snapshot.view:null;
 const state=view?.attempt?.state??view?.state;
 const review=state==='ESCALATED';
 const confirmed=!!view?.order_id;
 const message=offline?'Connection interrupted · last known evidence':!view?'Reading checkout evidence':confirmed?'Order confirmed by the backend':review?'Merchant review required':state==='UNKNOWN'?'Outcome unknown · do not pay again':`Backend state: ${state}`;
 const panel=<aside className="kernel-monitor" aria-label="Transaction kernel evidence"><header><ShieldCheck size={22}/><div><small>TRANSACTION TRUST KERNEL</small><strong>Every step has a boundary.</strong></div></header><div className="kernel-monitor-orbit" data-active={!!view&&!offline&&!confirmed} aria-hidden="true"><ShieldCheck size={30}/><i/></div><div className="kernel-monitor-row"><Fingerprint size={17}/><div><strong>Exact checkout</strong><span>{view?`Version ${view.current_version} · server record`:'Waiting for server record'}</span></div></div><div className="kernel-monitor-row"><ShieldCheck size={17}/><div><strong>Controlled execution</strong><span>{view?.attempt?'Execution attempt recorded':'No execution attempt reported'}</span></div></div><div className="kernel-monitor-row"><Activity size={17}/><div><strong>Provider evidence</strong><span>{confirmed?'Order recorded':view?.attempt?`Reported: ${view.attempt.state}`:'Not yet reported'}</span></div></div><output>{message}</output><footer>Browser return alone never confirms payment.</footer></aside>;
 return <>{!floatingOnly&&panel}{providerOpen&&typeof document!=='undefined'&&createPortal(<><div className="journey-provider-side journey-provider-left" aria-hidden="true">{view?.approval_card?<PaymentBill quote={view.approval_card.quote} amount={view.approval_card.amount_minor} method="razorpay"/>:<aside className="journey-bill"><output>Reading the approved bill…</output></aside>}</div><div className="journey-provider-side journey-provider-right" aria-hidden="true"><PaymentKernelFlow method="razorpay" phase={paymentAnimationState(state??'PROCESSING',view?.order_id,offline)} admitted={!!view?.attempt}/></div></>,document.body)}</>;
}
