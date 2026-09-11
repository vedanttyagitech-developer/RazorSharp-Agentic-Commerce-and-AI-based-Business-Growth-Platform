'use client';
import Link from 'next/link';
import {SimulatedOrders} from '@/components/simulated-orders';
import { ShoppingShowcase } from '@/components/shopping-showcase';
import { LiveMerchantInsights } from '@/components/live-merchant-insights';
import { LiveMerchantPolicy } from '@/components/live-merchant-policy';
import { LiveMerchant } from '@/components/live-merchant';
import { CampaignPlacement } from '@/components/discovery-cards';
import { useMotionState } from '@/components/continuity';
import { VoiceSessionProvider,useVoiceSession } from '@/components/voice-session';
import { useState,useEffect } from 'react';
import { MotionToggle } from '@/components/motion';
import { ArrowRight,ArrowUpRight,LayoutDashboard,Package,ShoppingBag,Headphones,FileCheck2,Activity,Send,Sparkles,ChevronDown,Check,TrendingUp,ShieldCheck,PanelRightClose,SlidersHorizontal,LockKeyhole,ChevronRight,Workflow,Store,Maximize2,Minimize2,Plus } from 'lucide-react';
import { SidebarProvider,Sidebar,SidebarContent,SidebarHeader,SidebarFooter,SidebarGroup,SidebarGroupLabel,SidebarMenu,SidebarMenuItem,SidebarMenuButton,SidebarInset,SidebarTrigger } from '@/components/ui/sidebar';
import { Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription } from '@/components/ui/dialog';
import { Brand,Badge,Composer,Primary,Proof,SectionHeading,InlineNote } from '@/components/concept';
import { initialActions,DemoAction,capabilities,type Product } from '@/lib/demo';
import { useCatalogue } from '@/lib/catalogue';
import { useMerchantFacts,copilotAnswer,topicOf } from '@/lib/merchant-facts';
import { useChatHistory } from '@/lib/use-chat-history';
import { useDemoResponse,ResponseActivity,ResponseVoice } from '@/components/response-motion';

const nav=[['overview','Action Center',LayoutDashboard],['growth','Business & growth',TrendingUp],['catalogue','Catalogue & inventory',Package],['orders','Orders',ShoppingBag],['campaigns','Campaigns',Send],['support','Customer Support Desk',Headphones],['policy','Merchant Policy',FileCheck2],['activity','Activity & proof',Activity],['operations','Operations',Workflow]] as const;
const skills=[['Business & Growth Analyst','What is driving this week’s growth?'],['Operations Assistant','Which products need restocking?'],['Pricing & Promotions','Suggest a weekend offer'],['Customer Support Desk','Help with the oldest customer case']];
export default function Merchant(){return <VoiceSessionProvider><MerchantWorkspace/></VoiceSessionProvider>}
function MerchantWorkspace(){
 const response=useDemoResponse();const voice=useVoiceSession();
 const [view,setView]=useMotionState('overview');const [copilot,setCopilot]=useState(true);
 // A conversation, not one slot. Every question used to overwrite the last, so a merchant
 // could not read back what they had already asked -- the buyer's copilot has kept a real
 // history all along, and this is the same hook it uses.
 const chat=useChatHistory();const turns=chat.active?.messages??[];
 const [wide,setWide]=useState(false);const [actions,setActions]=useState(initialActions);const [selected,setSelected]=useMotionState<DemoAction|null>(null);
 useEffect(()=>{const frame=requestAnimationFrame(()=>{if(window.location.hash==='#support')setView('support')});return()=>cancelAnimationFrame(frame)},[setView]);
 // The merchant's own shelf, from the store. It used to be a copy of eight products
 // kept in the front end, so this table could disagree with the shop next door.
 const {products}=useCatalogue();
 // What the copilot is allowed to quote. Every figure it states comes from here or from
 // `catalogue` below; nothing in its replies is a literal any more.
 const facts=useMerchantFacts();
 const [catalogue,setCatalogue]=useState<Product[]>([]);
 const [previousProducts,setPreviousProducts]=useState(products);if(previousProducts!==products){setPreviousProducts(products);setCatalogue(products)}const [proof,setProof]=useState(false);const [selectedOrder,setSelectedOrder]=useState('');
 const [safeMode,setSafeMode]=useState(false);const [protocol,setProtocol]=useState(false);const [notice,setNotice]=useState('');
 // The answer is saved with the turn rather than recomputed on every render: a chat log
 // that silently rewrites what it already said is not a log. It also means an older answer
 // keeps the figures that were true when it was given.
 const ask=(text:string)=>{
  voice.interrupt();response.start();setCopilot(true);
  chat.reply(chat.append(text),copilotAnswer(text,facts,catalogue));
  // One routing decision, shared with the answer, so the panel behind the copilot can
  // never end up showing a different subject from the one it just replied about.
  const panel={campaign:'campaigns',stock:'catalogue',support:'support',pricing:'policy',sales:null}[topicOf(text)];
  if(panel)setView(panel);
 };
 const createAction=(title:string,kind:string,before:string,after:string)=>{const a:DemoAction={id:`MA-${1043+actions.length}`,title,kind,before,after,status:'Awaiting approval',by:'You + Merchant Copilot',time:'Just now'};setActions(v=>[a,...v]);setSelected(a)};
 const applyAction=()=>{if(!selected||selected.status!=='Awaiting approval')return;setActions(v=>v.map(a=>a.id===selected.id?{...a,status:'Applied'}:a));if(selected.kind==='Inventory'){const p=catalogue.find(p=>selected.title.includes(p.name));if(p)setCatalogue(v=>v.map(x=>x.id===p.id?{...x,stock:parseInt(selected.after)}:x))}setSelected({...selected,status:'Applied'})};
 return <SidebarProvider style={{'--sidebar-width':'235px'} as React.CSSProperties} className="merchant-app">
  <Sidebar className="merchant-sidebar"><SidebarHeader className="merchant-brand"><Brand/><span className="merchant-subbrand">MERCHANT COMMAND</span></SidebarHeader><SidebarContent><div className="merchant-store"><span className="store-avatar"><Store size={18}/></span><div><strong>Green Basket</strong><span>Indiranagar, Bengaluru</span></div><ChevronDown size={13}/></div><SidebarGroup><SidebarGroupLabel>WORKSPACE</SidebarGroupLabel><SidebarMenu>{nav.map(([id,label,Icon])=><SidebarMenuItem key={id}><SidebarMenuButton isActive={view===id} onClick={()=>setView(id)} className="merchant-nav-button"><Icon size={17}/><span>{label}</span>{id==='overview'&&<span className="nav-count">{actions.filter(a=>a.status==='Awaiting approval').length}</span>}</SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu></SidebarGroup><SidebarGroup><SidebarGroupLabel>FUTURE CHANNELS</SidebarGroupLabel><SidebarMenu><SidebarMenuItem><SidebarMenuButton onClick={()=>setNotice('WhatsAppAdapter is reserved for future integration. Sending is disabled in this concept.')}><Send size={17}/><span>WhatsApp</span><Badge>Later</Badge></SidebarMenuButton></SidebarMenuItem></SidebarMenu></SidebarGroup></SidebarContent><SidebarFooter><div className="sidebar-trust"><ShieldCheck size={19}/><strong>Your business. Your say.</strong><p>Every consequential change starts with your approval.</p></div><Link className="workspace-switch" href="/"><span className="avatar">V</span><div><strong>Vedant’s workspace</strong><span>Back to platform</span></div><ArrowUpRight size={16}/></Link></SidebarFooter></Sidebar>
  <SidebarInset className="merchant-inset"><header className="merchant-topbar"><div><SidebarTrigger/><span className="breadcrumb">Workspace <ChevronRight size={13}/> <b>{nav.find(n=>n[0]===view)?.[1]}</b></span></div><div><MotionToggle/><Badge tone="amber">Simulated workspace</Badge><button className={`copilot-toggle ${copilot?'active':''}`} onClick={()=>setCopilot(!copilot)}><Sparkles size={16}/> Copilot</button><button className="avatar" aria-label="Workspace profile" onClick={()=>setNotice('You are exploring a sample merchant session. All records and actions in this concept are simulated.')}>V</button></div></header>
  <div className={`merchant-body ${copilot?'copilot-visible':''}`}><main className="merchant-main" key={view}>
   {(view==='overview'||view==='growth')&&<LiveMerchantInsights/>}
   {view==='catalogue'&&<LiveMerchant view="catalogue"/>}
   {view==='orders'&&<SimulatedOrders merchant/>}
   {view==='campaigns'&&<><SectionHeading eyebrow="STOREFRONT PREVIEW" title="Storefront placements" description="Preview a placement inside the shopping conversation. Email campaigns are not available."/><CampaignPlacement/></>}
   {view==='support'&&<><SimulatedOrders merchant support/><LiveMerchant view="support"/></>}
   {view==='policy'&&<LiveMerchantPolicy/>}
   {view==='activity'&&<LiveMerchant view="activity"/>}
   {view==='operations'&&<><SectionHeading eyebrow="THE QUIET WORK BEHIND THE EXPERIENCE" title="Clarity, even when things get complicated." description="Inspect outcomes, recovery work and integration boundaries."/><div className="ops-grid"><section className="panel"><div className="panel-heading"><h3>Safe Mode</h3><Badge tone={safeMode?'amber':'green'}>{safeMode?'Enabled in demo':'Normal in demo'}</Badge></div><p className="muted">Pause new financial admissions while reconciliation remains available.</p><button className="secondary" onClick={()=>setSafeMode(!safeMode)}>{safeMode?'Resume demo admissions':'Enable demo Safe Mode'}</button></section><section className="panel"><h3>Payment reconciliation</h3><div className="ops-value">1 <span>sample outcome to check</span></div><button className="secondary" onClick={()=>setSelectedOrder('RS-0909-B7K9')}>Inspect payment <ArrowUpRight size={15}/></button></section><section className="panel"><h3>Action Executor</h3><div className="ops-value">0 <span>failed sample commands</span></div><p className="muted">Leased → executing → evidence recorded</p></section><section className="panel"><h3>Agent & protocol access</h3><p className="muted">ACP, MCP and signed protocol evidence stay separate from buyer consent.</p><button className="secondary" onClick={()=>setProtocol(true)}>Explore capabilities <ArrowUpRight size={15}/></button></section></div><section className="panel"><h3>What this concept covers</h3><div className="capability-list">{capabilities.map(([a,b,c])=><div key={a}><span><strong>{a}</strong><small>{b}</small></span><Badge tone={c==='Available backend'?'green':c==='Future / disabled'?'neutral':'amber'}>{c}</Badge></div>)}</div></section></>}
   <footer className="merchant-foot">Connected merchant workspace <span>Storefront and operations previews are labelled separately.</span></footer>
  </main>
  {copilot&&<aside className={`merchant-copilot ${wide?'is-wide':''}`}>
   <header><div><strong>Your business copilot</strong><span>Context: {nav.find(n=>n[0]===view)?.[1]}</span></div><div className="copilot-header-actions">
    {turns.length>0&&<button className="icon-button" aria-label="Start a new conversation" title="New conversation" onClick={()=>{response.reset();chat.open(null)}}><Plus size={16}/></button>}
    <button className="icon-button" aria-label={wide?'Narrow the copilot':'Widen the copilot'} title={wide?'Narrow':'Widen'} aria-pressed={wide} onClick={()=>setWide(w=>!w)}>{wide?<Minimize2 size={15}/>:<Maximize2 size={15}/>}</button>
    <button className="icon-button" aria-label="Close copilot" onClick={()=>setCopilot(false)}><PanelRightClose size={17}/></button>
   </div></header>
   <div className="copilot-conversation">
    {/* The showcase and the welcome are an empty state, not furniture: once there is a
        conversation they would push every answer below the fold on a 305px panel. */}
    {!turns.length&&<><ShoppingShowcase merchant onSelect={ask}/><div className="copilot-welcome"><h3>A second pair of eyes.<br/>A few steps ahead.</h3><p>Let’s turn what’s happening in your business into what happens next.</p></div></>}
    {turns.map((turn,index)=>{
     const last=index===turns.length-1;
     // Older turns keep the answer they were given. Only the newest one waits on the
     // response animation, and only while it is still running.
     const show=last?response.hasAnswer:true;
     return <div className="copilot-turn" key={turn.id}>
      <div className="user-bubble">{turn.user}</div>
      {last&&!response.hasAnswer&&<ResponseActivity phase={response.phase} merchant/>}
      {show&&turn.reply&&<div className="copilot-reply"><span className="agent-name"> Merchant Copilot <Badge>Demo</Badge></span><p>{turn.reply}</p><div className="source-chip"><FileCheck2 size={13}/> {facts.loading?'Reading your records…':'Your backend records · read on open'}</div>{last&&<ResponseVoice text="This is a sample business brief. Review the proposed action and its exact changes before approving."/>}</div>}
     </div>;
    })}
    {!turns.length&&<div className="copilot-suggestions"><span>PUT YOUR COPILOT TO WORK</span>{skills.map(([s,q],i)=><button key={s} onClick={()=>ask(q)}><span className="skill-icon">{[<TrendingUp size={16} key="1"/>,<Send size={16} key="2"/>,<Package size={16} key="3"/>,<SlidersHorizontal size={16} key="4"/>,<Headphones size={16} key="5"/>][i]}</span>{s}<ArrowUpRight size={13}/></button>)}</div>}
   </div>
   <div className="copilot-compose"><Composer compact phase={response.phase} onStop={response.stop} onSend={ask} placeholder="Ask about your business…"/><p><LockKeyhole size={11}/> Proposes freely. Acts with your approval.</p></div>
  </aside>}
  </div></SidebarInset>
  <Dialog open={!!selected} onOpenChange={v=>!v&&setSelected(null)}><DialogContent className="concept-dialog"><DialogHeader><DialogTitle>{selected?.title}</DialogTitle><DialogDescription>{selected?.id} · proposed by {selected?.by}</DialogDescription></DialogHeader>{selected&&<><Badge tone={selected.status==='Applied'?'green':'amber'}>{selected.status}</Badge><div className="action-diff" key={selected.status}><div><span>BEFORE</span><strong>{selected.before}</strong></div><ArrowRight size={19}/><div><span>AFTER</span><strong>{selected.after}</strong></div></div><InlineNote>{selected.kind==='Merchant Policy'?'Affected open checkouts may need fresh buyer approval. Existing sale terms stay bound.':'You are approving this exact change against the current sample catalogue revision.'}</InlineNote><details className="technical-details"><summary>Approval details</summary><p>Sample revision: 18<br/>Content binding: {selected.id}-draft-v1<br/>Execution: simulated only</p></details>{selected.status==='Awaiting approval'?<><Primary onClick={applyAction}>Approve & apply in demo <Check size={16}/></Primary><div className="dialog-actions"><button className="subtle" onClick={()=>{setActions(v=>v.map(a=>a.id===selected.id?{...a,status:'Rejected'}:a));setSelected(null)}}>Reject proposal</button><button className="subtle" onClick={()=>{setActions(v=>v.map(a=>a.id===selected.id?{...a,status:'Stale'}:a));setSelected({...selected,status:'Stale'})}}>Simulate stale revision</button></div></>:selected.status==='Stale'?<><p className="muted">The store changed since this proposal was written. It cannot execute under the old approval.</p><Primary onClick={()=>createAction(selected.title,selected.kind,selected.before,selected.after)}>Create fresh proposal</Primary></>:<p className="muted">{selected.status==='Applied'?'Change recorded in the sample activity history. No real merchant data was modified.':'This proposal was declined.'}</p>}</>}</DialogContent></Dialog>

  <Dialog open={!!selectedOrder} onOpenChange={v=>!v&&setSelectedOrder('')}><DialogContent className="concept-dialog"><DialogHeader><DialogTitle>{selectedOrder}</DialogTitle><DialogDescription>Payment and order context · sample record</DialogDescription></DialogHeader><Badge tone={selectedOrder.includes('B7K9')?'amber':'green'}>{selectedOrder.includes('B7K9')?'Outcome being checked':'Payment confirmed in demo'}</Badge><div className="form-summary"><span>Payment attempt</span><strong>pa_demo_1042</strong><span>Provider Receipt</span><strong>provider_demo_1042</strong><span>Next step</span><strong>{selectedOrder.includes('B7K9')?'Reconcile before retry':'Prepare order'}</strong></div><InlineNote>{selectedOrder.includes('B7K9')?'Unknown does not mean failed. Do not submit a second payment.':'An order is confirmed by provider evidence, not an agent’s reply.'}</InlineNote><button className="secondary" onClick={()=>{setSelectedOrder('');setProof(true)}}>Open transaction proof</button></DialogContent></Dialog>
  <Dialog open={proof} onOpenChange={setProof}><DialogContent className="concept-dialog proof-dialog"><DialogHeader><DialogTitle>Evidence, in the open.</DialogTitle><DialogDescription>A sample view of the Transaction Trust Kernel proof chain.</DialogDescription></DialogHeader><Proof title="Every important step has a record."/></DialogContent></Dialog>
  <Dialog open={protocol} onOpenChange={setProtocol}><DialogContent className="concept-dialog"><DialogHeader><DialogTitle>Ways into the platform</DialogTitle><DialogDescription>Integration capability map, not live connection status.</DialogDescription></DialogHeader>{[['ACP','Checkout session transport'],['MCP','Capability-scoped tools'],['AP2 / UCP','Signed authorization and evidence'],['Voice','Ticketed gateway and grounded speech']].map(([a,b])=><div className="proof-row" key={a}><Workflow size={20}/><div><strong>{a}</strong><p>{b}</p></div><Badge>Inspect backend</Badge></div>)}<InlineNote>Protocol access does not grant an agent buyer consent.</InlineNote></DialogContent></Dialog>
  <Dialog open={!!notice} onOpenChange={v=>!v&&setNotice('')}><DialogContent className="concept-dialog"><DialogHeader><DialogTitle>Workspace preview</DialogTitle><DialogDescription>{notice}</DialogDescription></DialogHeader></DialogContent></Dialog>
 </SidebarProvider>;
}
