'use client';
import {useState,type CSSProperties} from 'react';
import Image from 'next/image';
import {Check,Package} from 'lucide-react';

export type ReserveConfirmedOrder={orderId:string;reference:string;items:{sku:string;name:string;quantity:number;image?:string}[]};

function ConfirmedItemImage({image,name}:{image?:string;name:string}){
 const [failed,setFailed]=useState(false);
 return image&&!failed?<Image src={image} alt={name} width={180} height={180} unoptimized loading="eager" onError={()=>setFailed(true)}/>:<span className="confirmed-item-placeholder"><Package size={38} aria-hidden="true"/><small>Image unavailable</small></span>;
}

/** This scene is mounted only for the matching backend-confirmed order. */
export function ReserveConfirmedItems({order}:{order:ReserveConfirmedOrder}){
 const quantity=order.items.reduce((sum,item)=>sum+item.quantity,0);
 if(!order.items.length)return null;
 return <section className="reserve-confirmed-items" aria-label="Confirmed order items">
  <header><div><span className="confirmed-items-kicker">YOUR FINDS, CONFIRMED</span><h4>{quantity} {quantity===1?'item':'items'}. All yours.</h4></div><span className="confirmed-items-reference">{order.reference}</span></header>
  <div className="confirmed-items-scene">
   <svg className="confirmed-tote-handles" viewBox="0 0 160 80" fill="none" aria-hidden="true"><path d="M34 77V40a46 46 0 0 1 92 0v37"/><path d="M45 77V41a35 35 0 0 1 70 0v36"/></svg>
   <ul className="confirmed-item-grid">{order.items.map((item,index)=><li className="confirmed-item-card" key={item.sku} style={{'--arrival':`${Math.min(index,8)*85}ms`,'--tilt':`${index%2===0?-3:3}deg`} as CSSProperties}><div className="confirmed-item-art"><ConfirmedItemImage key={item.image??item.sku} image={item.image} name={item.name}/><span className="confirmed-item-tick" aria-hidden="true"><Check size={12}/></span><span className="confirmed-item-quantity" aria-label={`Quantity ${item.quantity}`}>×{item.quantity}</span></div><strong>{item.name}</strong></li>)}</ul><span className="glass-bag-seal"><Check size={12}/> Order confirmed</span>
  </div>
 </section>;
}
