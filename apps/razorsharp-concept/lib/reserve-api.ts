import type {Product} from './demo';
import {ensureBuyerSession} from './commerce';
export type Permission={authorization_evidence?:{status:"VERIFIED"|"REAUTHORIZATION_REQUIRED";algorithm:string|null;payload_sha256:string|null;provider_reference:string|null;issuer_kind:"SIMULATOR"};authority_id:string;epoch:number;status:string;allowed_skus:string[]|null;per_purchase_limit_minor:number;capacity_minor:number;allocated_minor:number;available_minor:number;expires_at:string|null;provider_mode:string};
export type ReviewCard={reservation?:{expires_at:string}|null;checkout_id:string;version:number;content_hash:string;amount_minor:number;currency:string;policy_receipt_hash:string;quote:{lines:{sku:string;name:string;quantity:number;unit_price_minor:number;subtotal_minor:number;tax_minor:number}[];items_subtotal_minor:number;items_tax_minor:number;delivery_fee_minor:number;delivery_tax_minor:number;discount_minor:number}};
export type PaymentStatus={attempt_id:string;status:string;allocation:string;order_id:string|null;provider_mode:string;authority_id:string};
// The product's own backend identity. This used to parse the image filename; product
// identity now lives on the product, so renaming an asset cannot rename a purchase.
export const skuOf=(p:Product)=>p.sku;
export class ReserveRequestError extends Error {
 constructor(readonly status:number,message:string){super(message);this.name='ReserveRequestError'}
}
export async function commerce<T>(path:string,method='GET',body?:unknown,key?:string):Promise<T>{
 if(typeof window!=='undefined')await ensureBuyerSession();
 const response=await fetch('/api/commerce/'+path,{method,headers:{'Content-Type':'application/json',...(method==='GET'?{}:{'Idempotency-Key':key||crypto.randomUUID()})},...(method==='GET'||method==='HEAD'||body===undefined?{}:{body:JSON.stringify(body)})});
 const data=await response.json();if(!response.ok){const error=data as {detail?:string;title?:string};throw new ReserveRequestError(response.status,error.detail||error.title||'Backend request refused')}return data as T;
}
export const permissions=()=>commerce<{authorities:Permission[]}>('reserve/authorities');

export type OrderSummary={order_id:string;reference:string;payment_attempt_id:string;state:string;amount_minor:number;created_at:string};
export type ReservePurchase=OrderSummary&{allocation:string;payment_status:string;authority_id:string};

// The buyer's Reserve purchases, assembled rather than fetched: there is no "list my
// reserve payments" route, so this reads the order list and then asks the Reserve payment
// route about each one. A 404 there is the answer, not a failure -- it means that order
// was paid some other way -- so those are dropped rather than surfaced as an error.
//
// Only settled purchases can appear. An attempt that has not become an order is invisible
// here, because orders are the only list this buyer can read. The screen says so rather
// than implying the list is everything.
export async function reservePurchases():Promise<ReservePurchase[]>{
 const {orders}=await commerce<{orders:OrderSummary[]}>('orders?limit=25');
 const settled=await Promise.allSettled(orders.map(async o=>{
  const p=await commerce<PaymentStatus>(`reserve/payments/${o.payment_attempt_id}`);
  return {...o,allocation:p.allocation,payment_status:p.status,authority_id:p.authority_id};
 }));
 // Only 404 means this order has no Reserve payment. Auth/network/provider failures
 // must remain visible instead of turning an outage into an empty purchase history.
 for(const result of settled){if(result.status==='rejected' && !(result.reason instanceof ReserveRequestError && result.reason.status===404))throw result.reason}
 return settled.flatMap(r=>r.status==='fulfilled'?[r.value as ReservePurchase]:[]);
}
