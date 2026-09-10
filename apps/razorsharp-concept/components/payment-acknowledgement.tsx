'use client';
import {useEffect,useState} from 'react';
import {Download,ShieldCheck,RefreshCw} from 'lucide-react';
import {rawCommerceCall,type Order} from '@/lib/commerce';

type Acknowledgement={order:Order;provider:string;mode:string;method:string|null;provider_status:string|null;provider_checked:boolean};
export function acknowledgementText(data:Acknowledgement):string {
 const {order:o}=data;
 if(!o.payment?.capture_evidence||!['WEBHOOK','PROVIDER_FETCH'].includes(o.payment.capture_evidence.kind))throw Error('Verified capture required');
 return ['RazorSharp · Payment acknowledgement',
  data.mode==='test'?'TEST MODE — NO REAL MONEY':data.mode==='simulation'?'SIMULATION — NO REAL MONEY':data.mode==='live'?'LIVE PAYMENT':'Payment mode not verified',
  'Platform-generated acknowledgement. Not a Razorpay-issued document or tax invoice.',
  ...(data.mode==='test'?['Demo billed to: Vedant Tyagi (Test Mode display identity)']:[]),
  `Order: ${o.reference}`,`Order ID: ${o.order_id}`,`Amount originally paid: ${new Intl.NumberFormat('en-IN',{style:'currency',currency:o.currency}).format(o.amount_minor/100)}`,
  `Order recorded: ${o.created_at}`,`Provider: ${data.provider}`,`Payment ID: ${o.payment.razorpay_payment_id??'Not recorded'}`,
  `Provider order ID: ${o.payment.razorpay_order_id??'Not recorded'}`,`Method: ${data.method??'Not available'}`,
  `Recorded payment state: ${o.payment.state}`,`Capture evidence: ${o.payment.capture_evidence.kind}`,
  `Capture timestamp: ${o.payment.capture_evidence.verified_at||'Not recorded'}`,
  `Provider status: ${data.provider_status??'Not freshly verified'}`,
  'Refund records: '+JSON.stringify(o.refunds),
  `Acknowledgement generated: ${new Date().toISOString()}`].join('\n');
}
export function PaymentAcknowledgement({orderId}:{orderId:string}){
 const [data,setData]=useState<Acknowledgement|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0),[busy,setBusy]=useState(true);
 const identity=orderId+':'+revision;const [previousIdentity,setPreviousIdentity]=useState(identity);if(previousIdentity!==identity){setPreviousIdentity(identity);setBusy(true);setData(null);setError('')}
 useEffect(()=>{const abort=new AbortController();
  rawCommerceCall<Acknowledgement>(`orders/${orderId}/payment-acknowledgement`,{signal:abort.signal}).then(value=>{acknowledgementText(value);setData(value)}).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!abort.signal.aborted)setBusy(false)});
  return()=>abort.abort();
 },[orderId,revision]);
 function download(){if(!data)return;const url=URL.createObjectURL(new Blob([acknowledgementText(data)],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`RazorSharp-payment-${data.order.reference.replace(/[^a-zA-Z0-9-]/g,'')}.txt`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
 return <section aria-label="Payment acknowledgement" className="payment-acknowledgement">
  <header><ShieldCheck size={22}/><div><small>PAYMENT RECORD</small><h3>Payment acknowledgement</h3></div></header>
  {busy&&<output>Checking recorded payment details…</output>}{error&&<p role="alert">{error}</p>}
  {data&&<><span className="ack-mode">{data.mode==='test'?'TEST MODE · NO REAL MONEY':data.mode==='simulation'?'SIMULATED · NO REAL MONEY':data.mode==='live'?'LIVE PAYMENT':'MODE NOT VERIFIED'}</span>
   <h2>{new Intl.NumberFormat('en-IN',{style:'currency',currency:data.order.currency}).format(data.order.amount_minor/100)}</h2><p>Original captured amount · {data.order.reference}</p>
   {data.mode==='test'&&<p><strong>Demo billed to: Vedant Tyagi</strong><br/><small>Test Mode display identity</small></p>}
   <dl>{[['Provider',data.provider],['Payment method',data.method??'Not available'],['Payment ID',data.order.payment?.razorpay_payment_id??'Not recorded'],['Provider order ID',data.order.payment?.razorpay_order_id??'Not recorded'],['Order recorded',new Date(data.order.created_at).toLocaleString()],['Recorded payment state',data.order.payment?.state??'Unknown'],['Provider status',data.provider_status??'Not freshly verified']].map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
   {!data.provider_checked&&<p>Showing recorded capture evidence. {data.mode==='simulation'?'This purchase used the simulator.':'Fresh Razorpay details are unavailable.'}</p>}
   {data.order.refunds.length>0&&<details><summary>Refund records ({data.order.refunds.length})</summary>{data.order.refunds.map(refund=><p key={refund.refund_id}>{new Intl.NumberFormat('en-IN',{style:'currency',currency:refund.currency}).format(refund.amount_minor/100)} · {refund.state.replaceAll('_',' ')}<br/><small>{refund.refund_id}</small></p>)}</details>}
   <button className="primary" onClick={download}><Download size={16}/> Download acknowledgement</button>
   <p><small>RazorSharp payment acknowledgement · not a tax invoice or a Razorpay-issued document.</small></p></>}
  <button className="subtle" disabled={busy} onClick={()=>setRevision(x=>x+1)}><RefreshCw size={14}/> Refresh payment details</button>
 </section>
}
