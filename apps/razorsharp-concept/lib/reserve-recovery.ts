import {commerce,type PaymentStatus} from './reserve-api';

// A response can be lost before attemptId reaches sessionStorage. Resolve the
// original checkout, never infer failure from missing local data or a timeout.
export async function reserveRequestSettled(checkoutId:string):Promise<boolean>{
 const checkout=await commerce<{checkout_id:string;state:string;order_id?:string|null;attempt?:{attempt_id:string}|null}>(`checkouts/${checkoutId}`);
 if(checkout.checkout_id!==checkoutId)throw new Error('Payment recovery returned a different checkout.');
 if(!checkout.attempt?.attempt_id)return false;
 const payment=await commerce<PaymentStatus>(`reserve/payments/${checkout.attempt.attempt_id}`);
 return (payment.status==='CAPTURED'&&!!payment.order_id&&checkout.order_id===payment.order_id)
  ||(['FAILED','EXPIRED'].includes(payment.status)&&payment.allocation==='RELEASED');
}
