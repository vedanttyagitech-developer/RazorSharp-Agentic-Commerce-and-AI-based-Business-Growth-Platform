'use client';

import Link from 'next/link';
import {useEffect,useRef,useState} from 'react';
import {ArrowRight,ArrowUpRight,AudioLines,BarChart3,BookOpen,Check,CheckCheck,ChevronRight,Code2,FileCheck2,Fingerprint,GitBranch,Layers3,LockKeyhole,RefreshCw,Search,ShieldCheck,ShoppingBag,Sparkles,Timer,TrendingUp,Workflow} from 'lucide-react';
import {VoiceWave} from './voice-wave';

const repo='https://github.com/vedanttyagitech-developer/RazorSharp-Agentic-Commerce-and-AI-based-Business-Growth-Platform';
const challenges=[
 {icon:Search,title:'Too many filters. Too little context.',body:'Start with an occasion, a budget or a spoken request. Let the shopping copilot help turn it into a selection.',tag:'DISCOVERY → INTENT'},
 {icon:TrendingUp,title:'An insight is not an action.',body:'Give merchants a place to investigate stock, prepare changes and review the next move—not just read another chart.',tag:'INSIGHT → PROPOSAL'},
 {icon:RefreshCw,title:'The price moved. The yes didn’t.',body:'Bind approval to the exact checkout. Relevant changes require a new review, even when the new total is lower.',tag:'CHANGED TERMS → FRESH APPROVAL'},
 {icon:CheckCheck,title:'A retry must not be a second purchase.',body:'Use idempotency and single-winner admission to protect the financial path from repeated requests.',tag:'RETRY → SAME OPERATION'},
 {icon:Timer,title:'No response is not a failed payment.',body:'Keep uncertain outcomes visible. Reconcile provider evidence instead of encouraging another payment.',tag:'UNCERTAINTY → RECOVERY'},
 {icon:Fingerprint,title:'“Done” needs something behind it.',body:'Connect the approved content, financial decision, execution and provider outcome through inspectable evidence.',tag:'CLAIM → CHECKABLE RECORD'},
];
const growth=[
 {name:'Understand',icon:BarChart3,title:'Find the signal behind the store.',body:'Bring catalogue health, inventory and business context into the same workspace. Separate measured records from illustrative analysis.',items:['Catalogue and stock context','Orders and outcome context','Evidence before recommendations'],result:'A question worth acting on',status:'Catalogue connected · analytics preview'},
 {name:'Prepare',icon:Sparkles,title:'Turn a question into a concrete proposal.',body:'Prepare a campaign, stock change or Merchant Policy offer that a merchant can inspect before anything takes effect.',items:['Readable before / after','Scope and affected products','Content ready for review'],result:'A reviewable next move',status:'Merchant workspace includes preview flows'},
 {name:'Approve',icon:FileCheck2,title:'Make the merchant’s yes precise.',body:'The Merchant Action Controller binds approval to a typed proposal and its source revision. A changed proposal needs a fresh decision.',items:['Canonical action content','Revision-aware review','Separate financial authority'],result:'A decision with boundaries',status:'Controller implemented · UI coverage varies'},
 {name:'Execute',icon:Workflow,title:'Let services do the consequential work.',body:'Approved operations belong in server-side services. Financial actions stay behind the Transaction Trust Kernel; channel sends belong to connectors.',items:['Explicit execution state','Failure and recovery context','No hidden model-side send'],result:'An outcome that can be inspected',status:'Email campaign design deferred'},
 {name:'Learn',icon:TrendingUp,title:'Connect the next move to its outcome.',body:'Read captured amounts and refunds from records. Show attribution honestly; a campaign-associated order is not proof of incremental revenue.',items:['Captured and refunded amounts','Traceable order context','Clearly labelled scenario results'],result:'Evidence for the next decision',status:'Growth measurement is still being connected'},
];
const scenarios=[
 {name:'Exact purchase',title:'Your yes belongs to these exact details.',before:'₹420',after:'₹420',status:'Ready for admission',tone:'blue',detail:'An approval names the checkout version, content hash, amount and currency. The Kernel checks the bound purchase before admitting financial work.',steps:['Review exact content','Record buyer authority','Revalidate and admit'],code:'CONTENT + AUTHORITY + CURRENT STATE'},
 {name:'Offer changes',title:'A better price still needs a fresh yes.',before:'₹420',after:'₹390',status:'Fresh approval required',tone:'orange',detail:'A Merchant Policy offer changes while checkout is open. Show the updated discount and amount; the previous approval must not silently carry forward.',steps:['Merchant terms change','Invalidate stale approval','Show delta and ask again'],code:'REAPPROVAL_REQUIRED'},
 {name:'Repeated request',title:'Two requests. One financial operation.',before:'Request A',after:'Retry A',status:'Recover the existing operation',tone:'violet',detail:'The same operation key and content recover the recorded result. Reusing a key with different content is a conflict—not permission for a second charge.',steps:['Receive operation key','Match request fingerprint','Return the recorded result'],code:'IDEMPOTENCY + SINGLE-WINNER ADMISSION'},
 {name:'Permission revoked',title:'A saved permission is not forever.',before:'Epoch 4',after:'Epoch 5',status:'New debit refused',tone:'red',detail:'Reserve authority is revocable. A stale authority epoch cannot admit new spending. Already accepted or uncertain provider work still needs reconciliation.',steps:['Revoke saved authority','Advance authority epoch','Refuse stale debit authority'],code:'REVOCATION + RECONCILIATION'},
];
const evidence=[
 ['Intent','Who proposed the purchase, with an attributable agent principal.'],
 ['Merchant snapshot','The authoritative merchant state used for the checkout version.'],
 ['Content hash','Canonical checkout content is hashed again, not trusted because a stored label says verified.'],
 ['Policy-at-Sale Receipt','The sale-bound Merchant Policy terms and their frozen evidence.'],
 ['Approval & authority','The exact version and authority epoch associated with approval.'],
 ['Kernel decision','The recorded financial admission or refusal decision.'],
 ['Grant & durable command','A single-use Execution Grant tied to the work that carries it.'],
 ['Provider request','A redacted reference to the attempted provider operation.'],
 ['Provider evidence','Callback, webhook or reconciliation evidence supporting the outcome.'],
 ['Final state','The resulting payment, refund and order state—not an agent’s success sentence.'],
];
const buyerRoles=[
 ['Shopping Specialist','Understands shopping intent and proposes grounded product selections.','Model-backed when configured'],
 ['Checkout Specialist','Coordinates deterministic checkout information and guarded purchase steps.','Deterministic runner'],
 ['Support Specialist','Uses order and sale-term context to support issue handling.','Deterministic runner'],
];
const merchantRoles=[
 ['Business & Growth Analyst','Catalogue health, inventory trends and growth questions.','Workspace skill · preview analysis'],
 ['Campaign Builder','Audience criteria, content and campaign review.','Campaign UI preview'],
 ['Operations Assistant','Stock, catalogue and merchant-setting proposals.','Controller-backed domain design'],
 ['Pricing & Promotions','Merchant Policy offers and reviewable pricing changes.','UI coverage varies'],
 ['Customer Support Desk','Customer cases, resolution review and escalation context.','Merchant-facing workspace'],
];
function Eyebrow({children}:{children:React.ReactNode}){return <span className="hl-eyebrow"><i/>{children}</span>}
export function PlatformHighlights(){
 const root=useRef<HTMLDivElement>(null);
 const [growthStep,setGrowthStep]=useState(0),[scenario,setScenario]=useState(0),[plane,setPlane]=useState<'buyer'|'merchant'>('buyer'),[link,setLink]=useState(2);
 useEffect(()=>{const nodes=root.current?.querySelectorAll<HTMLElement>('[data-hl-reveal]');if(!nodes)return;const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.setAttribute('data-hl-reveal','visible');observer.unobserve(entry.target)}}),{threshold:.08});nodes.forEach(n=>{n.setAttribute('data-hl-reveal','pending');observer.observe(n)});return()=>observer.disconnect()},[]);
 const g=growth[growthStep],s=scenarios[scenario];
 return <div className="platform-highlights" ref={root}>
  <section id="highlights" className="hl-section hl-problems" data-hl-reveal>
   <div className="hl-heading"><div><Eyebrow>THE CHALLENGES WE TAKE SERIOUSLY</Eyebrow><h2>Commerce is easy.<br/><em>Until something changes.</em></h2></div><p>A customer changes their mind. A merchant changes an offer. A network drops after payment. RazorSharp is built around the decisions hiding between the happy-path screens.</p></div>
   <div className="hl-challenge-grid">{challenges.map(({icon:Icon,title,body,tag},i)=><article className="hl-challenge" key={title} style={{'--hl-index':i} as React.CSSProperties}><div className="hl-challenge-top"><Icon size={23}/><span>0{i+1}</span></div><h3>{title}</h3><p>{body}</p><small>{tag}</small></article>)}</div>
  </section>
  <section className="hl-section hl-growth" data-hl-reveal>
   <div className="hl-heading"><div><Eyebrow>FOR THE BUSINESS BEHIND THE STORE</Eyebrow><h2>Less dashboard watching.<br/><em>More considered action.</em></h2></div><p>Merchant Command brings investigation, proposals and approvals into one workspace. The goal: help merchants move from “what happened?” to “what should I do next?”</p></div>
   <div className="hl-growth-shell"><div className="hl-growth-nav" aria-label="Explore the merchant workflow">{growth.map(({name,icon:Icon},i)=><button key={name} aria-pressed={i===growthStep} onClick={()=>setGrowthStep(i)}><span className="hl-step-number">0{i+1}</span><Icon size={19}/><strong>{name}</strong><ChevronRight size={16}/></button>)}</div>
    <div className="hl-growth-detail" key={growthStep}><span className="hl-status">{g.status}</span><h3>{g.title}</h3><p>{g.body}</p><ul>{g.items.map(item=><li key={item}><Check size={15}/>{item}</li>)}</ul><div className="hl-growth-outcome"><span>NEXT OUTCOME</span><strong>{g.result}<ArrowUpRight size={22}/></strong></div><small>Interactive product-flow illustration · not live analytics</small></div>
   </div>
  </section>
  <section id="innovations" className="hl-kernel-section" data-hl-reveal>
   <div className="hl-kernel-grid" aria-hidden="true"/>
   <div className="hl-section"><div className="hl-heading"><div><Eyebrow>THE TRANSACTION TRUST KERNEL</Eyebrow><h2>Intelligence can propose.<br/><em>Authority must be proven.</em></h2></div><p>Merchant terms govern the sale. Independent platform invariants govern the money. Neither a model response nor a merchant approval can bypass the financial checks.</p></div>
    <div className="hl-scenarios" aria-label="Explore transaction controls">{scenarios.map((item,i)=><button aria-pressed={i===scenario} onClick={()=>setScenario(i)} key={item.name}>{item.name}</button>)}</div>
    <div className={`hl-transaction hl-${s.tone}`} key={scenario}><div className="hl-transaction-copy"><span className="hl-demo-label">ILLUSTRATIVE SCENARIO · NO TRANSACTION EXECUTED</span><h3>{s.title}</h3><p>{s.detail}</p><code>{s.code}</code></div><div className="hl-transaction-visual"><div className="hl-amounts"><div><span>BEFORE</span><strong>{s.before}</strong></div><ArrowRight size={22}/><div><span>AFTER</span><strong>{s.after}</strong></div></div><div className="hl-decision"><ShieldCheck size={21}/>{s.status}</div><ol>{s.steps.map((step,i)=><li key={step}><span>{i+1}</span>{step}</li>)}</ol></div></div>
    <div className="hl-invariants">{[['Approval binding','Exact content, amount and currency'],['Single-use grants','Execution authority cannot be replayed'],['Refund limits','Bounded by refundable paid amounts'],['Safe recovery','Unknown outcomes remain unresolved']].map(([title,body])=><div key={title}><LockKeyhole size={17}/><strong>{title}</strong><span>{body}</span></div>)}</div>
   </div>
  </section>
  <section className="hl-section hl-architecture" data-hl-reveal>
   <div className="hl-heading"><div><Eyebrow>TWO COPILOTS. CLEAR RESPONSIBILITIES.</Eyebrow><h2>Meet Razor AI.<br/><em>Two copilots. Defined roles.</em></h2></div><p>Specialists help with different jobs. Trusted services own consequential actions. The boundary is part of the product, not a sentence hidden in a prompt.</p></div>
   <div className="hl-agent-layout"><div className="hl-voice-card"><AudioLines size={27}/><span>VOICE + TEXT</span><h3>Say what you need.<br/>Keep the context.</h3><VoiceWave mode="listening"/><p>Speech recognition, conversation and spoken responses share a gateway path. Connection and provider configuration determine availability.</p><div><ShieldCheck size={16}/> Voice is an interface—not financial authority.</div></div>
    <div className="hl-agent-panel"><div className="hl-plane-switch" aria-label="Choose copilot architecture"><button aria-pressed={plane==='buyer'} onClick={()=>setPlane('buyer')}><ShoppingBag size={17}/> Shopping copilot</button><button aria-pressed={plane==='merchant'} onClick={()=>setPlane('merchant')}><BarChart3 size={17}/> Merchant Command</button></div><div className="hl-roles" key={plane}>{(plane==='buyer'?buyerRoles:merchantRoles).map(([title,body,status],i)=><article key={title}><span className="hl-role-number">0{i+1}</span><div><h3>{title}</h3><p>{body}</p><small>{status}</small></div><GitBranch size={17}/></article>)}</div><div className="hl-boundary"><Layers3 size={18}/><span>{plane==='buyer'?'Buyer approval → Transaction Trust Kernel → Action Executor':'Merchant approval → Merchant Action Controller → domain services'}</span></div></div>
   </div>
  </section>
  <section className="hl-section hl-evidence" data-hl-reveal>
   <div className="hl-heading"><div><Eyebrow>ENGINEERING YOU CAN INSPECT</Eyebrow><h2>Follow the purchase.<br/><em>All the way to the evidence.</em></h2></div><p>Ten links connect intent to the final financial state. The verifier recomputes content hashes and checks audit chains. Missing evidence stays missing.</p></div>
   <div className="hl-evidence-layout"><div className="hl-evidence-list" aria-label="Explore the ten evidence links">{evidence.map(([title],i)=><button key={title} aria-pressed={link===i} onClick={()=>setLink(i)}><span>{String(i+1).padStart(2,'0')}</span>{title}<ChevronRight size={16}/></button>)}</div><div className="hl-evidence-detail"><div className="hl-evidence-orbit" aria-hidden="true"><Fingerprint size={68}/><i/><i/></div><span className="hl-demo-label">EVIDENCE MODEL · NOT A LIVE VERDICT</span><div key={link} className="hl-evidence-text"><small>LINK {String(link+1).padStart(2,'0')} / 10</small><h3>{evidence[link][0]}</h3><p>{evidence[link][1]}</p></div><div className="hl-honest-state"><BookOpen size={17}/><span>Explaining a check is different from verifying a purchase.</span></div><a href={`${repo}/blob/main/packages/commerce-api/src/commerce_api/services/proof_chain.py`} target="_blank" rel="noreferrer">Inspect the verifier <ArrowUpRight size={17}/></a></div></div>
  </section>
  <section className="hl-section hl-protocols" data-hl-reveal>
   <div className="hl-heading"><div><Eyebrow>BUILT FOR AGENTIC COMMERCE</Eyebrow><h2>Open interfaces.<br/><em>Explicit boundaries.</em></h2></div><p>Protocol support and controlled execution belong together. An adapter is not a marketplace listing, and a simulated debit is not a live bank payment.</p></div>
   <div className="hl-protocol-grid">{[
    ['ACP','Commerce transport','ACP-compatible checkout interfaces. Not a live ChatGPT Instant Checkout integration.',Code2],
    ['UCP','Profiles & signed interactions','Version-pinned UCP support with explicit claim boundaries. Not availability inside Gemini.',GitBranch],
    ['MCP / AP2','Tools & authorization evidence','Capability-scoped tools and protocol evidence. Protocol access does not create buyer consent.',Workflow],
    ['Reserve / UAP','Bounded delegated spending','Selected-product authority, limits, epoch and revocation. Provider execution is simulated; NPCI/banks are not connected.',ShieldCheck],
   ].map(([title,label,body,Icon])=>{const Glyph=Icon as typeof ShieldCheck;return <article key={String(title)}><Glyph size={24}/><strong>{String(title)}</strong><span>{String(label)}</span><p>{String(body)}</p></article>})}</div>
   <div className="hl-track-note"><span>AI GROWTH & AGENTIC COMMERCE</span><p>Merchant insight-to-action, conversational discovery, checkout controls and inspectable payment outcomes—the project’s contribution to the Track 1 theme. This is a build in progress, not a claim of certification or complete rubric coverage.</p></div>
  </section>
  <section className="hl-section hl-finale" data-hl-reveal><span className="hl-final-mark"><Sparkles size={30}/></span><Eyebrow>EXPLORE THE PRODUCT. INSPECT THE SYSTEM.</Eyebrow><h2>Thoughtful on the surface.<br/><em>Accountable underneath.</em></h2><p>Two ways into commerce. One commitment to keeping consequential actions within their authority.</p><div><Link className="hl-cta" href="/shop">Try the shopping copilot <ArrowRight size={17}/></Link><Link className="hl-cta hl-secondary" href="/merchant">Open Merchant Command <ArrowUpRight size={17}/></Link></div><a className="hl-source" href={repo} target="_blank" rel="noreferrer"><Code2 size={16}/> Explore the public source <ArrowUpRight size={14}/></a></section>
 </div>;
}
