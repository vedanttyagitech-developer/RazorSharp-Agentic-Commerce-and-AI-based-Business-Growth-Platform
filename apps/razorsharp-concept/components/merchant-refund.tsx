'use client';
import {useEffect,useState} from 'react';
import {RefundApprovalPanel} from './refund-approval-panel';
import {merchantCall} from './live-merchant';
type Review={approval_hash:string;refundable_minor:number;currency:string;explanation:string;anything_remains:boolean};
type Approval={case_id:string;reason:string;amount_minor:number;approval_hash:string};
type Pending={key:string;body:Approval};
const money=(minor:number,currency='INR')=>new Intl.NumberFormat('en-IN',{style:'currency',currency}).format(minor/100);
export function MerchantRefund({caseId,orderId,reason}:{caseId:string;orderId:string;reason:string}){
 const storageKey=`merchant-refund:${caseId}`;
 const [review,setReview]=useState<Review|null>(null),[pending,setPending]=useState<Pending|null>(null),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
 useEffect(()=>{const frame=requestAnimationFrame(()=>{try{const saved=sessionStorage.getItem(storageKey);if(saved){const value=JSON.parse(saved) as Pending;if(value.body.case_id===caseId){setPending(value);setMessage('An approval may already be processing. Check status or retry the same approval.')}}}catch{setMessage('Could not restore the previous approval. Check order status before approving again.')}});return()=>cancelAnimationFrame(frame)},[caseId,storageKey]);
 async function refresh(){setBusy(true);try{const data=await merchantCall(`orders/${orderId}/refundable`) as unknown as Review;setReview(data);setMessage(data.explanation||'Review the case and the exact amount before approving.')}catch(e){setMessage((e as Error).message)}finally{setBusy(false)}}
 async function status(){setBusy(true);try{const data=await merchantCall(`orders/${orderId}`) as unknown as {refunds?:{refund_id:string;amount_minor:number;state:string}[]};setMessage(data.refunds?.length?data.refunds.map(r=>`${r.refund_id}: ${money(r.amount_minor)} · ${r.state}`).join('\n'):'No recorded refund yet. An unresolved request must be retried with the same approval.')}catch(e){setMessage((e as Error).message)}finally{setBusy(false)}}
 async function approve(approvedMinor?:number){if(busy)return;setBusy(true);try{
  let intent=pending;
  if(!intent){if(!review||approvedMinor===undefined||!Number.isSafeInteger(approvedMinor)||approvedMinor<=0||approvedMinor>review.refundable_minor)throw Error('Review a valid refund amount first.');const minor=approvedMinor;intent={key:crypto.randomUUID(),body:{case_id:caseId,reason,amount_minor:minor,approval_hash:review.approval_hash}};sessionStorage.setItem(storageKey,JSON.stringify(intent));setPending(intent);}
  const data=await merchantCall(`orders/${orderId}/refunds`,'POST',intent.body,intent.key) as unknown as {decision:{allowed:boolean;code:string};refund?:{state:string;refund_id:string}};
  setMessage(data.decision.allowed?`Refund admitted: ${data.refund?.refund_id} · ${data.refund?.state}. Provider completion is still pending.`:`Not admitted: ${data.decision.code}.`);
  sessionStorage.removeItem(storageKey);setPending(null);setReview(null);
 }catch(e){const status=(e as Error & {status?:number}).status;if(status&&([401,403,404,422].includes(status)||['Refund review changed','Case changed','Provider refund unavailable'].includes((e as Error & {title?:string}).title||''))){sessionStorage.removeItem(storageKey);setPending(null);setReview(null);setMessage(`${(e as Error).message}. Review again before submitting a new approval.`)}else setMessage(`${(e as Error).message}. Check status; do not create a second approval while the outcome is unknown.`)}finally{setBusy(false)}}
 return <section aria-label="Merchant refund approval"><p>Financial action · Razorpay payments only. Resolving a case does not move money.</p><button className="secondary" disabled={busy||!!pending} onClick={()=>void refresh()}>Review refund</button><button className="subtle" disabled={busy} onClick={()=>void status()}>Check refund status</button>{review&&!pending&&<RefundApprovalPanel key={review.approval_hash} maximum={review.refundable_minor} busy={busy} onApprove={minor=>void approve(minor)} onCancel={()=>setReview(null)}/>}{pending&&<button className="secondary" disabled={busy} onClick={()=>void approve()}>Retry same approved refund {money(pending.body.amount_minor)}</button>}{message&&<output style={{whiteSpace:'pre-wrap'}}>{message}</output>}</section>;
}
