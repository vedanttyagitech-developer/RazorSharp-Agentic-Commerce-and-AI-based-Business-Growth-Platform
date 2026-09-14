'use client';
import {useEffect,useRef,useState,type ReactNode} from 'react';
import {createPortal} from 'react-dom';
import frameStyles from './reserve-transfer.css?inline';

// Isolate the animation's styles without creating another browser/payment frame.
const styles=frameStyles.replaceAll('html[data-theme=dark]','[data-theme=dark]').replaceAll('html[data-motion=paused]','[data-motion=paused]');
export function ReservePaymentFrame({children,title='Reserve Pay payment flow'}:{children:ReactNode;title?:string}){
 const host=useRef<HTMLDivElement>(null);
 const [mount,setMount]=useState<HTMLElement|null>(null);
 useEffect(()=>{
  if(!host.current)return;
  const shadow=host.current.shadowRoot??host.current.attachShadow({mode:'open'});
  const style=document.createElement('style');
  style.textContent=styles+'\n:host{display:block}*{box-sizing:border-box}svg{flex-shrink:0}h3{padding:0}.payment-scene-root{font-family:Inter,ui-sans-serif,system-ui,sans-serif}.reserve-transfer{border:0;border-radius:0}';
  const root=document.createElement('div');root.className='payment-scene-root';
  shadow.replaceChildren(style,root);
  const sync=()=>{root.dataset.theme=document.documentElement.dataset.theme??'light';root.dataset.motion=document.documentElement.dataset.motion??'full'};
  sync();const observer=new MutationObserver(sync);observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme','data-motion']});
  setMount(root);
  return()=>{observer.disconnect();shadow.replaceChildren()};
 },[]);
 return <div className="reserve-payment-frame" aria-label={title}><div ref={host}/>{mount&&createPortal(children,mount)}</div>;
}
