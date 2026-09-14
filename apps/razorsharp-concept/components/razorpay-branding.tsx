'use client';
import {useEffect,useState} from 'react';
import {rawCommerceCall} from '@/lib/commerce';
import './payment-animation.css';
export function RazorpayBranding(){
 const [mode,setMode]=useState<'test'|'live'|'unknown'>('unknown');
 useEffect(()=>{const abort=new AbortController();void rawCommerceCall<{razorpay_mode?:string}>('config',{signal:abort.signal}).then(data=>{if(!abort.signal.aborted)setMode(data.razorpay_mode==='test'?'test':data.razorpay_mode==='live'?'live':'unknown')}).catch(()=>{});return()=>abort.abort()},[]);
 return <div><div className="powered-razorpay"><span>Powered by</span><strong>Razorpay</strong><small>{mode==='test'?'Test mode':mode==='live'?'Live mode':'Checking payment mode'}</small></div><small className="powered-razorpay-note">Razorpay Checkout · Reserve Pay uses a separate simulated provider.</small></div>;
}
