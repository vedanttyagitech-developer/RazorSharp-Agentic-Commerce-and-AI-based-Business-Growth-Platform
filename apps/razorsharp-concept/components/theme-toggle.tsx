'use client';
import {useEffect,useState} from 'react';
import {Moon,Sun} from 'lucide-react';

export function ThemeToggle(){
 const [dark,setDark]=useState(false);
 useEffect(()=>{const sync=()=>setDark(document.documentElement.dataset.theme==='dark');sync();window.addEventListener('razorsharp:theme',sync);return()=>window.removeEventListener('razorsharp:theme',sync)},[]);
 return <button type="button" className="theme-toggle" aria-label={dark?'Switch to light mode':'Switch to dark mode'} title={dark?'Light mode':'Dark mode'} aria-pressed={dark} onClick={()=>{const next=dark?'light':'dark';document.documentElement.dataset.theme=next;document.documentElement.classList.toggle('dark',next==='dark');try{localStorage.setItem('razorsharp:theme',next)}catch{/* Theme still works when storage is unavailable. */}window.dispatchEvent(new Event('razorsharp:theme'))}}>{dark?<Sun size={17}/>:<Moon size={17}/>}</button>;
}
