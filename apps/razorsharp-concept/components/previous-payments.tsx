'use client';
import {useEffect,useState} from 'react';
import {commerce,type CheckoutView} from '@/lib/commerce';
import {recoveryMessage,canResumeManualCheckout,needsPaymentAttention} from '@/lib/checkout-recovery';
import {CheckoutRecovery} from './checkout-recovery';
import {Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription} from './ui/dialog';

/** Earlier purchases are independent of today's cart. Never confirm or clear that cart. */
export function PreviousPayments({onOrder,onConfirmed}:{onOrder:()=>void;onConfirmed:()=>void}){
 const [rows,setRows]=useState<CheckoutView[]>([]),[selected,setSelected]=useState<CheckoutView|null>(null),[error,setError]=useState('');
 useEffect(()=>{const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
 const refresh=async()=>{try{const found:CheckoutView[]=[];let cursor:string|undefined;do{const page=await commerce.checkout.list({limit:100,cursor,signal:controller.signal});for(const row of page.checkouts){if(row.state==='PAID')continue;const view=await commerce.checkout.read(row.checkout_id,controller.signal);if(needsPaymentAttention(view))found.push(view)}cursor=page.next_cursor??undefined}while(cursor&&!controller.signal.aborted);if(!controller.signal.aborted){setRows(found);setError('')}}catch(e){if(!controller.signal.aborted)setError(`Could not check earlier payments: ${(e as Error).message}`)}finally{if(!controller.signal.aborted)timer=setTimeout(refresh,15000)}};
 void refresh();return()=>{controller.abort();clearTimeout(timer)}},[]);
 async function open(id:string){try{setSelected(await commerce.checkout.read(id));setError('')}catch(e){setError((e as Error).message)}}
 if(!rows.length&&!error&&!selected)return null;
 return <section aria-label="Previous payments" style={{margin:'4px 0',padding:10,border:'1px solid #e9b970',borderRadius:16,background:'#fff8ec'}}>
 <details><summary style={{cursor:"pointer"}}><strong>{rows.length} earlier {rows.length===1?"payment needs":"payments need"} attention</strong> · Review</summary><p>These payments still need a verified outcome. Your current shopping cart stays separate.</p>
 {error&&<p role="alert">{error}</p>}
 {rows.map(row=><article key={row.checkout_id} style={{padding:'12px 0',borderTop:'1px solid #ecd9bd'}}>
  <strong>{row.attempt?.state==='ESCALATED'?'Needs merchant review':row.state==='PAYMENT_FAILED'||row.attempt?.state==='FAILED'?'Payment failed':canResumeManualCheckout(row)?'Checkout left unfinished':'Payment being checked'}</strong>
  <p>{recoveryMessage(row)}</p>
  <details><summary>Payment reference</summary><small>Checkout {row.checkout_id}</small></details>
  {row.approval_card&&<><ul aria-label="Earlier purchase items">{row.approval_card.quote.lines.map(line=><li key={line.sku}>{line.name} × {line.quantity}</li>)}</ul><strong>Reviewed bill · {new Intl.NumberFormat('en-IN',{style:'currency',currency:row.approval_card.currency}).format(row.approval_card.amount_minor/100)}</strong></>}
  <div><button className="secondary" onClick={()=>void open(row.checkout_id)}>{canResumeManualCheckout(row)?'View original checkout':'View payment status'}</button></div>
 </article>)}
 </details>
 <Dialog open={!!selected} onOpenChange={open=>{if(!open)setSelected(null)}}><DialogContent><DialogHeader><DialogTitle>Earlier purchase · locked cart</DialogTitle><DialogDescription>We use the original checkout and payment attempt. Your current cart stays separate.</DialogDescription></DialogHeader>{selected&&<CheckoutRecovery key={selected.checkout_id} initial={selected} onOrder={()=>{setSelected(null);onOrder()}} onConfirmed={onConfirmed}/>}</DialogContent></Dialog>
 </section>;
}
