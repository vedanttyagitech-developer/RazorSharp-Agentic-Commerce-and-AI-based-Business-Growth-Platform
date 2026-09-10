'use client';
import {useState} from 'react';
import {money} from '@/lib/demo';
export function refundAmountMinor(input:string,maximum:number):number|null{
 if(!/^\d+(\.\d{1,2})?$/.test(input))return null;
 const [whole,fraction='']=input.split('.');const minor=Number(whole)*100+Number(fraction.padEnd(2,'0'));
 return Number.isSafeInteger(minor)&&minor>0&&minor<=maximum?minor:null;
}
/** Shared human approval surface; execution stays in the caller's real or demo adapter. */
export function RefundApprovalPanel({maximum,busy=false,simulation=false,onApprove,onCancel}:{maximum:number;busy?:boolean;simulation?:boolean;onApprove:(minor:number)=>void;onCancel?:()=>void}){
 const [amount,setAmount]=useState((maximum/100).toFixed(2));
 const minor=refundAmountMinor(amount,maximum);
 return <div className="sim-refund-review"><strong>{simulation?'Review simulated refund':'Review refund'}</strong><p>Maximum refundable: {money(maximum)}. Review the case and approve the exact amount.</p><label>Refund amount (₹)<input aria-label={simulation?'Simulated refund amount (₹)':'Refund amount (₹)'} inputMode="decimal" value={amount} disabled={busy} onChange={e=>setAmount(e.target.value)}/></label>{minor===null&&<p role="alert">Enter an amount above zero, within the refundable limit, with at most two decimals.</p>}<button className="primary" disabled={busy||minor===null} onClick={()=>{if(minor!==null)onApprove(minor)}}>{busy?'Submitting…':`Approve ${simulation?'simulated ':''}refund ${minor===null?'':money(minor)}`}</button>{onCancel&&<button className="subtle" disabled={busy} onClick={onCancel}>Back</button>}<p>{simulation?'Simulation only. No Razorpay request is sent.':'Approval starts processing; only verified provider status confirms completion.'}</p></div>;
}
