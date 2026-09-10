'use client';
import {useEffect,useState} from 'react';
import {Clock3,TriangleAlert} from 'lucide-react';
import {commerce,type CheckoutView} from '@/lib/commerce';

/** Server deadline plus monotonic elapsed time; neither reload nor local clock extends it. */
export function PaymentWindow({checkoutId}:{checkoutId:string}) {
 const [snapshot,setSnapshot]=useState<{view:CheckoutView;remaining:number;at:number}|null>(null);
 const [tick,setTick]=useState(0);const [now,setNow]=useState(0);
 useEffect(()=>{
  let stopped=false,timer:ReturnType<typeof setTimeout>;
  const read=async()=>{try{const view=await commerce.checkout.read(checkoutId);const a=view.attempt;
   if(!stopped)setSnapshot({view,remaining:a?.payment_window_expires_at&&a.server_now?Math.max(0,Date.parse(a.payment_window_expires_at)-Date.parse(a.server_now)):0,at:performance.now()});
  }catch{/* Keep the last deadline through disconnect; never restart the clock. */}
  finally{if(!stopped)timer=setTimeout(read,2000)}};
  void read();const pulse=setInterval(()=>{setTick(v=>v+1);setNow(performance.now())},250);
  return()=>{stopped=true;clearTimeout(timer);clearInterval(pulse)};
 },[checkoutId]);
 const a=snapshot?.view.attempt;
 if(!snapshot||!a?.payment_window_expires_at||snapshot.view.order_id||['CAPTURED','FAILED','REFUNDED','PARTIALLY_REFUNDED'].includes(a.state))return null;
 const seconds=Math.max(0,Math.ceil((snapshot.remaining-(Math.max(0,now-snapshot.at)))/1000));
 const closed=!!a.window_closed||seconds===0;
 const unstarted=a.state==='EXPIRED';
 return <div className="payment-window" data-urgent={!closed&&seconds<=30} data-closed={closed} data-tick={tick%2}>
  <div className="payment-window-heading">{closed?<TriangleAlert size={18}/>:<Clock3 size={18}/>}<strong>{closed?'Payment window closed':'Complete payment before this checkout closes.'}</strong><span aria-label={`${seconds} seconds remaining`}>{Math.floor(seconds/60)}:{String(seconds%60).padStart(2,'0')}</span></div>
  <output>{closed?(unstarted?'Payment was not executed. Stock has been released; review again to start a new checkout.':'Checking payment status. Do not pay again; a late debit will be reconciled against this purchase.'):seconds<=30?'Less than 30 seconds left. Finish your payment now.':'Your items are held for this payment window. Reopening does not restart it.'}</output>
  {!closed&&<div className="payment-window-track" aria-hidden="true"><i style={{transform:`scaleX(${seconds/120})`}}/></div>}
 </div>;
}
