'use client';
import Image from 'next/image';
import Link from 'next/link';
import {ImpactDeck} from '@/components/impact-deck';
import {TrustBoundaries} from '@/components/trust-boundaries';
import {PlatformHighlights} from '@/components/platform-highlights';
import {IdentityHero} from '@/components/identity-hero';
import { MotionToggle } from '@/components/motion';
import { ArrowUpRight, ArrowRight, ShoppingBag, Command, ShieldCheck, Sparkles, AudioLines } from 'lucide-react';
function Tilt({ children, className = '' }: {children: React.ReactNode; className?: string}) {
  return <Link href={className.includes('merchant') ? '/merchant' : '/shop'} className={`portal ${className}`} onPointerMove={e => { const b=e.currentTarget.getBoundingClientRect(); e.currentTarget.style.setProperty('--rx', `${-(e.clientY-b.top-b.height/2)/65}deg`); e.currentTarget.style.setProperty('--ry', `${(e.clientX-b.left-b.width/2)/65}deg`); }} onPointerLeave={e=>{e.currentTarget.style.setProperty('--rx','0deg');e.currentTarget.style.setProperty('--ry','0deg');}}>{children}</Link>;
}
export default function Home() {
 return <div className="platform-page has-identity">
  <header className="platform-header"><Link className="wordmark branded-wordmark" href="/">razorsharp<span className="platform-brand-label">platform</span></Link><div className="platform-motion"><MotionToggle/><span className="prototype-pill"><i/> Meet Razor AI</span></div><nav className="hl-header-nav" aria-label="Platform navigation"><a href="#why-razorsharp">20 features</a><a href="#track-one">Track 1</a><a href="#highlights">Highlights</a><a href="#trust-boundaries">Trust & boundaries</a><a className="hl-nav-open" href="#copilots">Explore copilots <ArrowUpRight size={15}/></a></nav></header>
  <IdentityHero/><ImpactDeck/><main className="platform-main" id="copilots">
   <div className="eyebrow"><span className="tiny-orbit"/> COMMERCE, IN CONVERSATION</div>
   <h1>Good things happen<br/>when intelligence <em>acts.</em></h1>
   <p className="platform-intro">Your next great find. Your business’s next big move.<br/>Powered by Razor AI. Built for a more thoughtful way to commerce.</p>
   <div className="portal-grid">
    <Tilt className="buyer-portal"><Image src="/quick-commerce-hero.png" alt="Quick-commerce assortment: electronics, personal care, home essentials, stationery, toys, snacks and groceries" className="portal-photo" unoptimized width={640} height={640}/><div className="portal-content"><span className="portal-kicker"><ShoppingBag size={16}/> FOR YOUR EVERYDAY</span><h2>A little less searching.<br/>A lot more finding.</h2><p>Everyday tech, beauty, home and groceries. One Razor AI shopping copilot.</p><span className="portal-cta">Meet your shopping copilot <ArrowUpRight size={21}/></span></div><span className="floating-note"><Sparkles size={15}/> “Earphones, skincare, and snacks for tonight.”</span></Tilt>
    <Tilt className="merchant-portal"><div className="merchant-art" aria-hidden="true"><div className="portal-story-cards"><span>01 / UNDERSTAND YOUR BUSINESS</span><strong>Turn insights into<br/>your next move.</strong><span>02 / REVIEW A CAMPAIGN</span><strong>A thoughtful hello.<br/>A reason to return.</strong></div><div className="float-metric"><span>From insight to action</span><strong>Your next move <ArrowUpRight size={18}/></strong><div className="mini-bars">{[25,42,35,56,49,68,58,85,76,100].map((h,i)=><i key={i} style={{height:h+'%'}}/>)}</div></div></div><div className="portal-content"><span className="portal-kicker"><Command size={16}/> FOR YOUR BUSINESS</span><h2>Your ambition.<br/>An unfair advantage.</h2><p>Turn business questions into your next move.</p><span className="portal-cta">Open Merchant Command <ArrowUpRight size={21}/></span></div><span className="art-caption">Illustrative business data</span></Tilt>
   </div>
   <div className="platform-footer"><span><ShieldCheck size={17}/> Intelligence proposes. You stay in control.</span><span><AudioLines size={17}/> Speak naturally. Shop thoughtfully.</span><span>Powered by possibility <ArrowRight size={16}/></span></div>
  </main><TrustBoundaries/><PlatformHighlights/><footer className="bottom-note">RazorSharp <span>Connected services and labelled previews · no live-bank Reserve integration</span><span>COMMERCE, WITH ACCOUNTABILITY</span></footer>
 </div>;
}
