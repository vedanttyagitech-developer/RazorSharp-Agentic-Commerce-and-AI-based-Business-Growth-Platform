import {ProjectTour} from '@/components/project-tour';
import { lazy, Suspense, useEffect, useState } from 'react';
import {VoiceSessionProvider} from '@/components/voice-session';
import { createRoot } from 'react-dom/client';
import { MotionPolicy } from '@/components/continuity';
import '../app/globals.css';
import '../app/integration-workspace.css';
import '../app/motion.css';
import '../app/voice-native.css';
import '../app/identity.css';
import '../app/continuity.css';
import '../app/reserve.css';
import '../app/discovery.css';
import '../app/visual-system.css';
import '../app/command-experience.css';
import '../app/reserve-india.css';
import '../app/product-polish.css';
import '../app/platform-highlights.css';
import '../app/impact-deck.css';
import '../app/trust-boundaries.css';
import '../app/transaction-kernel.css';
import '../app/theme.css';
try { const saved=localStorage.getItem('razorsharp:theme'); const theme=saved==='dark'||saved==='light'?saved:window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'; document.documentElement.dataset.theme=theme; document.documentElement.classList.toggle('dark',theme==='dark'); } catch { document.documentElement.dataset.theme='light'; }
const pages = {
  '/': lazy(() => import('../app/page')),
  '/ecommerce-store': lazy(() => import('../app/shop/page')),
  '/merchant': lazy(() => import('../app/merchant/page')),
  '/platform': lazy(() => import('../app/platform/page')),
};
const path = window.location.pathname.replace(/\/$/, '') || '/';
const legacyStore = path === '/shop';
if (legacyStore) window.location.replace('/ecommerce-store/' + window.location.search + window.location.hash);
function MountedApp(){
 const [current,setCurrent]=useState(path);
 useEffect(()=>{const navigate=(event:Event)=>{const next=(event as CustomEvent<string>).detail;if(!(next in pages))return;event.preventDefault();window.history.pushState(null,'',next==='/'?'/':next+'/');setCurrent(next)};const back=()=>setCurrent(window.location.pathname.replace(/\/$/,'')||'/');window.addEventListener('razorsharp:navigate',navigate);window.addEventListener('popstate',back);return()=>{window.removeEventListener('razorsharp:navigate',navigate);window.removeEventListener('popstate',back)}},[]);
 const Page=pages[current as keyof typeof pages];
 return <VoiceSessionProvider key={current}><MotionPolicy/><ProjectTour/><Suspense fallback={<output>Opening RazorSharp…</output>}>{legacyStore?<output>Opening Ecommerce Store…</output>:Page?<Page/>:<h1>Page not found</h1>}</Suspense></VoiceSessionProvider>;
}
createRoot(document.getElementById('root')!).render(<MountedApp/>);
