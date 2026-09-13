'use client';
import {useEffectEvent} from 'react';
import Image from 'next/image';
import Link from 'next/link';
import {useChatHistory} from '@/lib/use-chat-history';
import {PreviousPayments} from '@/components/previous-payments';
import {shopNavigation,shoppingView} from '@/lib/shop-navigation';
import {requestsCheckoutReview} from '@/lib/checkout-choice';
import {canRefreshCheckout,clearCancelledCheckoutRecovery,isSpentCheckout} from '@/lib/checkout-recovery';
import {SimulatedOrders} from '@/components/simulated-orders';
import {DurableCart} from '@/lib/durable-cart';
import {commerce,totalMinor, type Cart} from '@/lib/commerce';
import type {VoiceOffer} from '@/lib/voice/client';
import { LiveOrders } from '@/components/live-orders';
import { VoiceSessionProvider,useVoiceSession } from '@/components/voice-session';
import { useEffect,useRef,useState } from 'react';
import { MotionToggle } from '@/components/motion';
import { ArrowRight,ArrowUpRight,ChevronDown,ShoppingBag,Plus,Minus,X,ShieldCheck,Package,Check,Headphones,Sparkles,LockKeyhole } from 'lucide-react';
import { Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription } from '@/components/ui/dialog';
import { transition,useMotionState,dropFromBasket,flyToBasket,captureProductMotion } from '@/components/continuity';
import { AccountControls } from '@/components/account-controls';
import {ProductFacts} from '@/components/product-facts';
import { ProductComparison } from '@/components/product-comparison';
import { ShoppingShowcase } from '@/components/shopping-showcase';

import { ReservePay } from '@/components/reserve-pay';
import type { ReserveConfirmation } from '@/components/reserve-checkout';
import type { ManualConfirmation } from '@/components/manual-checkout';
import { skuOf } from '@/lib/reserve-api';
import { OrderReview } from '@/components/order-review';
import { SidebarProvider,Sidebar,SidebarContent,SidebarHeader,SidebarFooter,SidebarGroup,SidebarGroupLabel,SidebarMenu,SidebarMenuItem,SidebarMenuButton,SidebarInset,SidebarTrigger } from '@/components/ui/sidebar';
import { Brand,Badge,Composer,ProductArt,ProductCard,Primary,Empty } from '@/components/concept';
import { money,Product } from '@/lib/demo';
import { useCatalogue } from '@/lib/catalogue';
import { type ResponsePhase,ResponseActivity,AnswerText } from '@/components/response-motion';

const beginTurn=()=>{};
type View='discover'|'orders'|'reserve'|'support';
type Stage='basket'|'approval'|'changed'|'checking'|'confirmed';
export default function Shop(){return <VoiceSessionProvider><ShopWorkspace/></VoiceSessionProvider>}
function ShopWorkspace(){
 const voice=useVoiceSession();
 const livePhase:ResponsePhase=voice.phase==='transcribing'?'thinking':voice.phase==='speaking'?'answering':voice.reply!==null?'complete':'idle';
 const response={phase:livePhase,hasAnswer:voice.reply!==null,start:beginTurn,stop:voice.reset,reset:voice.reset};
 // The store's own shelf: 247 products with the store's prices and stock, not the eight
 // that used to be written down in `lib/demo.ts` beside the real ones.
 const {products,loading:catalogueLoading,error:catalogueError}=useCatalogue();
 const [supportOrder,setSupportOrder]=useState<string|undefined>();
 const [view,setView]=useMotionState<View>('discover');const chat=useChatHistory();const messages=chat.active?.messages??[];const query=messages.at(-1)?.user??'';const responseTarget=useRef<{chatId:string;messageId:string}|null>(null);const [search,setSearch]=useState('');
 useEffect(()=>{const frame=requestAnimationFrame(()=>{if(window.location.hash==='#reserve')setView('reserve');else if(window.location.hash==='#support')setView('support');else if(window.location.hash==='#orders')setView('orders')});return()=>cancelAnimationFrame(frame)},[setView]);
 const turns=messages.slice(0,-1);
 const persistReply=chat.reply;
 useEffect(()=>{if(voice.reply!==null&&responseTarget.current)persistReply(responseTarget.current,voice.reply)},[voice.reply,persistReply]);
 const [paymentLocked,setPaymentLocked]=useState(false);
 const [reviewOpen,setReviewOpen]=useState(false);const [reviewRequested,setReviewRequested]=useState(false);const paymentPending=useRef(false);
 const [orderSnapshot,setOrderSnapshot]=useState<{orderId?:string;total:number;basket:Record<string,number>;paid:Record<string,number>}|null>(null);
 const [storedCart,setStoredCart]=useState<Cart|null>(null);const [cartReady,setCartReady]=useState(false);const [cartError,setCartError]=useState('');const [cartBusy,setCartBusy]=useState(0);
 const [cartStore]=useState(()=>new DurableCart(setStoredCart));
 const basket:Record<string,number>=Object.fromEntries((storedCart?.lines??[]).map(l=>[l.sku,l.quantity]));
 useEffect(()=>{let active=true;cartStore.restore().then(()=>{if(active)setCartReady(true)}).catch(e=>{if(active)setCartError(e.message)});return()=>{active=false}},[cartStore]);
 const refreshCart=()=>{setCartError('');void cartStore.restore().then(()=>setCartReady(true)).catch(e=>setCartError(e.message))};
 const startNewCart=(ready?:()=>void)=>{setCartBusy(v=>v+1);void cartStore.newCart().then(()=>{setStage('basket');setPaymentLocked(false);setCartReady(true);setCartError('');ready?.()}).catch(e=>setCartError(e.message)).finally(()=>setCartBusy(v=>v-1))};
 const freshReview=async(checkoutId:string)=>{
  const view=await commerce.checkout.read(checkoutId);
  if(!canRefreshCheckout(view))throw Error('Payment may be in progress. Check its status before replacing this checkout.');
  // A live checkout is cancelled first: that is what releases its stock hold and retires the
  // version, and replacing one without it would leak the reservation. A spent checkout has
  // already had both done to it, and asking again is refused -- which is what stranded the
  // buyer on an EXPIRED bill with nothing to press.
  if(!isSpentCheckout(view)){
   const decision=await commerce.checkout.cancel(checkoutId,crypto.randomUUID());
   if(!decision.allowed)throw Error(decision.explanation||'The checkout cannot be replaced yet.');
  }
  clearCancelledCheckoutRecovery(checkoutId);
  const items=[...(storedCart?.lines??[])];setCartBusy(v=>v+1);
  try{await cartStore.newCart();for(const item of items)await cartStore.change(item.sku,item.quantity);setPaymentLocked(false);setStage('basket');setReviewOpen(false);setCartOpen(true)}finally{setCartBusy(v=>v-1)}
 };
 const [cartOpen,setCartOpen]=useState(false);const [stage,setStage]=useState<Stage>('basket');
 const [product,setProduct]=useMotionState<Product|null>(null);const [proof,setProof]=useState(false);const [question,setQuestion]=useState('');
// Support is a VIEW now, not a dialog. `LiveOrders support` opens against the buyer's real
// orders and reads `orders/{id}/support-cases` from the backend, so a case is raised against
// an order that exists. The dialog it replaced was still here, unreachable -- nothing called
// its setter -- and still building `orderId` as 'RS-0908-A4F2' with paid amounts taken from
// catalogue prices, so a case could carry a reference to no order and a figure nobody paid.
// Dead code that fabricates an order identity is one refactor away from being live again.
 const quote=storedCart?.quote;
 const lines=products.filter(p=>basket[p.id]);const count=Object.values(basket).reduce((a,b)=>a+b,0);const subtotal=quote?.items_subtotal_minor??0;const discount=quote?.discount_minor??0;const delivery=quote?.delivery_fee_minor??0;const total=quote?totalMinor(quote):0;
 const changeSequence=useRef(0);const [basketUpdates,setBasketUpdates]=useState<{id:number;name:string;delta:number;quantity:number}[]>([]);
 const add=(id:string,amount=1,source?:HTMLElement,proposal?:VoiceOffer)=>{if(!cartReady||paymentLocked||stage==='checking'||stage==='confirmed'){const message=!cartReady?'Your basket is still loading. Please try again when it is ready.':'This basket is locked for checkout. Close review and start a new cart before adding more items.';setCartError(message);if(proposal)voice.reportCartResult(message);return false}const item=products.find(p=>p.id===id);if(!item){const message='That product is not available in the current catalogue. Please choose a current product; your cart has not changed.';setCartError(message);if(proposal)voice.reportCartResult(message);return false}
 const originElement=source??document.querySelector<HTMLElement>(`[data-product="${CSS.escape(id)}"]`);const motionOrigin=originElement?captureProductMotion(originElement):null;
 setCartBusy(v=>v+1);setCartError('');
 void cartStore.change(id,amount,proposal).then(result=>{
 if(motionOrigin){if(result.delta<0)dropFromBasket(motionOrigin);else if(result.delta>0)flyToBasket(motionOrigin)}
 if(result.delta>0&&result.cart.sales_event_id&&!reviewOpen){voice.cartUpdated(result.cart.cart_id,result.cart.sales_event_id)}else if(proposal){voice.reportCartResult(`${item.name}: ${result.quantity} now in your basket.`)}
 setStage(reviewOpen?'approval':'basket');const event={id:++changeSequence.current,name:item.name,delta:result.delta,quantity:result.quantity};setBasketUpdates(v=>[...v.slice(-3),event]);
 }).catch(e=>{setCartError(e.message);if(proposal)voice.reportCartResult(`The cart update was not confirmed. ${e.message}`)}).finally(()=>setCartBusy(v=>v-1));return true};
 // What the buyer types goes to the assistant, the same one that answers when they speak.
 //
 // It used to go nowhere. `send` matched a few words with regular expressions, switched a
 // view and set a local search string -- so the typed conversation was a simulation sitting
 // beside a real one, and anything the phrasebook did not recognise silently became a
 // catalogue filter. The regexes below stay ONLY as navigation: "my orders" should open
 // the orders view without a round trip, and that is a shortcut rather than an answer.
 // A shortcut changes the VIEW; it never swallows the sentence. Everything the buyer types
 // reaches the assistant, because voice and typing are one conversation with one context on
 // the server, and a message that only moved a view is a message the assistant never heard --
 // which is how "track my order" spoken and "track my order" typed came to mean different
 // things in the same session.
 const navigateShop=(text:string)=>{const action=shopNavigation(text);if(!action)return false;voice.interrupt();setReviewRequested(false);setReviewOpen(false);setCartOpen(action==='basket');setView('discover');voice.speak(action==='basket'?'Here is your basket.':paymentLocked?'Checkout view closed. Your started payment is still being checked; this does not cancel it.':'Order review closed. Your items remain in your basket.');return true};
 const send=(text:string)=>{if(navigateShop(text))return;if(requestsCheckoutReview(text)){voice.interrupt();setReviewRequested(true);return}responseTarget.current=chat.append(text);setView('discover');
  const route=shoppingView(text);
  if(route)setView(route);else response.start();
  // Over the socket when one is open, so the reply is spoken as well as shown. Otherwise
  // open one: a typed conversation does not need a microphone, and since a declined
  // microphone no longer closes the session, this works with speech switched off.
  // `say` queues when the socket is still opening, so nothing is lost to a race.
  voice.say(text)};
 // Final voice transcripts already reached the server. Only update the surface here;
 // calling send() would submit the same purchase intent twice.
 const displayedVoiceTurn=useRef(0);
 const handleVoiceTurn=useEffectEvent(()=>{const turn=voice.finalTurn;if(!turn||turn.sequence===displayedVoiceTurn.current)return;
  if(!reviewOpen&&requestsCheckoutReview(turn.text)&&(!cartReady||cartBusy))return;
  displayedVoiceTurn.current=turn.sequence;if(navigateShop(turn.text))return;if(reviewOpen)return;if(requestsCheckoutReview(turn.text)){voice.interrupt();setReviewRequested(true);return}responseTarget.current=chat.append(turn.text);setSearch('');setView('discover');response.start();
 });
 useEffect(()=>{const frame=requestAnimationFrame(()=>handleVoiceTurn());return()=>cancelAnimationFrame(frame)},[voice.finalTurn,reviewOpen,cartReady,cartBusy]);
 const newChat=()=>{responseTarget.current=null;setBasketUpdates([]);voice.interrupt();response.reset();chat.open(null);setSearch('');setView('discover')};
 const matches=products.filter(p=>!search||(search==='fruit'?p.category==='Produce':p.name.toLowerCase().includes(search.toLowerCase())));
 // The runway is a strip the buyer swipes, and it was built for a handful of picks. The
 // shelf is 247 products now, which laid end to end is a hundred thousand pixels of
 // sideways scrolling -- so an unsearched view shows an opening selection and says how
 // many more there are. A search shows every match, because a buyer who asked for
 // something specific has already narrowed it themselves.
 const RUNWAY=24;
 // A voice turn names the products it put on the page, and those are what the buyer was
 // just told about -- so they are the shelf until the conversation moves on. Without this
 // the assistant said "here are your options" and the screen showed something else.
 const spoken=(voice.reply!==null||voice.items.length>0)?voice.items.map(i=>products.find(p=>p.sku===i.sku)).filter((p):p is Product=>!!p):null;
 // A typed search always wins. Without this the shelf stayed on whatever the last voice
 // turn named, so searching after speaking showed the previous answer's products --
 // stale, and indistinguishable from a search that found them.
 const shown=search?matches:(spoken??matches.slice(0,RUNWAY));
 const beyond=matches.length-shown.length;

 const reviewOrder=()=>{if(!count||!cartReady||cartBusy||cartError)return;responseTarget.current=null;voice.checkoutGuidance(undefined);if(stage!=='checking'&&stage!=='confirmed')setStage('approval');setReviewOpen(true);setCartOpen(false)};
 // Both payment methods end here, and both end here because the BACKEND said so: an order
 // id exists only where verified capture evidence put it. The per-item shares below are a
 // rendering of the quote the backend already priced, never a total this page worked out.
 const finishReviewRequest=useEffectEvent(()=>{if(!reviewRequested||!cartReady||cartBusy)return;setReviewRequested(false);if(cartError)return;if(!count){setCartError('Your cart is empty. Add an item before reviewing checkout.');return}reviewOrder()});
 useEffect(()=>{const frame=requestAnimationFrame(()=>finishReviewRequest());return()=>cancelAnimationFrame(frame)},[reviewRequested,cartReady,cartBusy,cartError,count]);
 const confirmPurchase=(orderId:string,card:{amount_minor:number;quote:{lines:{sku:string;quantity:number;subtotal_minor:number;tax_minor:number}[];items_subtotal_minor:number;discount_minor:number}})=>{const paid:Record<string,number>={};const confirmedBasket:Record<string,number>={};let allocated=0;const q=card.quote;q.lines.forEach((line,i)=>{const product=products.find(p=>skuOf(p)===line.sku);if(!product)return;const share=i===q.lines.length-1?q.discount_minor-allocated:Math.floor(q.discount_minor*line.subtotal_minor/q.items_subtotal_minor);allocated+=share;paid[product.id]=line.subtotal_minor+line.tax_minor-share;confirmedBasket[product.id]=line.quantity});setOrderSnapshot({orderId,total:card.amount_minor,basket:confirmedBasket,paid});setStage('confirmed');setPaymentLocked(false);paymentPending.current=false};
 const reserveConfirmed=(result:ReserveConfirmation)=>confirmPurchase(result.orderId,result.card);
 const manualConfirmed=(result:ManualConfirmation)=>confirmPurchase(result.orderId,result.card);
 // A spoken "add two milk" reaches here as a proposal the server marked as one. Executing
 // it is this surface's job -- the agent proposes and never writes -- and it is the buyer's
 // own instruction, so there is no second yes. Adding to a basket is not consent to buy:
 // approval and payment stay where they are, behind the review screen.
 //
 // Cleared through `takeProposal` as soon as it is applied, so a re-render cannot add it
 // twice -- which with `add`'s delta semantics would be two more packets, not the same two.
 const appliedProposal=useRef<object|null>(null);
 useEffect(()=>{if(!cartReady||catalogueLoading)return;const p=voice.proposal;if(!p||appliedProposal.current===p)return;
  // Keyed on the proposal object itself, not on a boolean. Clearing it is a setState,
  // and an effect that re-ran before that landed would add the units a second time --
  // which with `add`'s delta semantics is two MORE packets, not the same two.
  appliedProposal.current=p;voice.takeProposal();add(p.sku,p.quantity,undefined,p)});
 const newChatEvent=useEffectEvent(newChat);
 useEffect(()=>{const handler=(e:KeyboardEvent)=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();newChatEvent()}};window.addEventListener('keydown',handler);return()=>window.removeEventListener('keydown',handler)},[]);
 useEffect(()=>{const context=(document as Document & {modelContext?:{registerTool:(tool:{name:string;description:string;inputSchema:object;annotations:object;execute:(input:{view:View})=>Promise<object>},options:{signal:AbortSignal})=>Promise<void>}}).modelContext;if(!context?.registerTool)return;const lifecycle=new AbortController();Promise.resolve(context.registerTool({name:'navigate_shopping_view',description:'Open a simulated shopping view. Does not authorize payment or mutate a basket.',inputSchema:{type:'object',properties:{view:{type:'string',enum:['discover','orders','reserve','support']}},required:['view'],additionalProperties:false},annotations:{readOnlyHint:false},execute:async(input:{view:View})=>{if(!['discover','orders','reserve','support'].includes(input?.view))throw Error('Invalid view');setView(input.view);return {view:input.view,mode:'simulation'}}},{signal:lifecycle.signal})).catch(()=>{});return()=>lifecycle.abort()},[setView]);
 return <SidebarProvider className="shopping-shell" style={{'--sidebar-width':'225px'} as React.CSSProperties}><Sidebar className="chat-sidebar"><SidebarHeader><Brand/><button className="new-chat" onClick={newChat}><Plus size={17}/> New conversation <span>⌘ K</span></button></SidebarHeader><SidebarContent><SidebarGroup><SidebarMenu>{([['discover','Your copilot',Sparkles],['orders','Your orders',Package],['reserve','Reserve Pay',LockKeyhole],['support','Get help',Headphones]] as const).map(([id,t,Icon])=><SidebarMenuItem key={id}><SidebarMenuButton isActive={view===id} onClick={()=>setView(id)}><Icon size={17}/><span>{t}</span></SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu></SidebarGroup><SidebarGroup><PreviousPayments onOrder={()=>setView('orders')} onConfirmed={()=>{}}/></SidebarGroup><SidebarGroup><SidebarGroupLabel>CONVERSATIONS</SidebarGroupLabel><small>Saved in this browser</small><div className="chat-history">{chat.history.chats.length===0?<p>Your saved conversations will appear here.</p>:[...chat.history.chats].reverse().map(c=><button key={c.id} aria-current={c.id===chat.history.activeId?'true':undefined} onClick={()=>{responseTarget.current=null;voice.interrupt();voice.reset();chat.open(c.id);setView('discover')}}>{c.title}</button>)}</div>{chat.notice&&<output>{chat.notice}</output>}</SidebarGroup></SidebarContent><SidebarFooter><div className="chat-sidebar-note"><ShieldCheck size={19}/><p>Your copilot can find it.<br/><strong>Only you can approve it.</strong></p></div><Link href="/" className="workspace-switch"><span className="avatar">V</span><div><strong>Vedant</strong><span>Back to platform</span></div><ArrowUpRight size={15}/></Link></SidebarFooter></Sidebar><SidebarInset><div data-detail-open={product?.id} className={`buyer-app chat-first ${!query&&view==='discover'?'empty-chat':''}`}>
  <header className="app-header"><div className="header-left"><SidebarTrigger/><span className="copilot-title">Shopping copilot <ChevronDown size={14}/></span></div><div className="header-right"><MotionToggle/><Badge tone="amber">Interactive concept</Badge><AccountControls/><button className="cart-toggle" onClick={()=>setCartOpen(!cartOpen)} aria-label={`Open cart, ${stage==='confirmed'?0:count} items`}><ShoppingBag size={19}/><span key={count}>{stage==='confirmed'?0:count}</span></button></div></header>
  {(!cartReady||cartBusy>0)&&<output>{cartReady?'Updating your cart…':'Restoring your cart…'}</output>}
  <section aria-label="Current shopping cart" style={{margin:'8px 24px',padding:'12px 16px',border:'1px solid #e2e8f0',borderRadius:14}}>
   <strong>Current cart</strong><p>{!cartReady?'Checking your saved cart…':paymentLocked?'This cart is locked for checkout. Close review to start separate shopping.':storedCart?`${count} items · Separate from earlier payments.`:'Start a new cart when you’re ready. Previous payment records stay separate.'}</p>
   {cartReady&&(!storedCart||!!cartError||(paymentLocked&&!reviewOpen))&&<button className="secondary" disabled={cartBusy>0} onClick={()=>startNewCart(()=>setCartOpen(true))}>Start a new cart</button>}
  </section>
  {reviewRequested&&<output>Preparing checkout—finishing your cart update…</output>}
  {cartError&&<div role="alert"><p>{cartError}</p><button onClick={()=>{setCartBusy(v=>v+1);void cartStore.retry().then(()=>{setCartError('');setCartReady(true)}).catch(e=>setCartError(e.message)).finally(()=>setCartBusy(v=>v-1))}}>Retry cart update</button><button onClick={refreshCart}>Refresh cart</button></div>}
  <div className={`buyer-layout ${cartOpen?'with-basket':''}`}>
   <main className="buyer-main">
   {view==='discover'&&<>
    <div className={`shop-intro ${query?'has-conversation':''}`}>
     {!query?<><div className="eyebrow">YOUR PERSONAL SHOPPING COPILOT</div><h1>What can I find <em>for you?</em></h1><p>A craving, a list, a half-formed idea. Start anywhere.</p></>:<>{turns.map((t,i)=><div className="previous-turn" key={i}><div className="user-bubble">{t.user}</div>{t.reply&&<p><Sparkles size={14}/> {t.reply}</p>}</div>)}<div className="user-bubble">{query}</div>{(voice.reply!==null||messages.at(-1)?.reply)&&<div className="agent-answer" key={query}><div><span className="agent-name">Razor AI <Badge>Shopping</Badge></span><AnswerText animate={false} text={voice.reply??messages.at(-1)?.reply??''}/><span className="source-note"><Check size={13}/> {voice.reply!==null?'Assistant response · store catalogue':'Saved reply · ask again for current prices and availability'}</span></div></div>}<ResponseActivity phase={response.phase} live/></>}
     {!query&&<><div className="main-composer"><Composer onSend={send} phase={response.phase} onStop={response.stop}/></div><ShoppingShowcase onSelect={text=>transition(()=>send(text))}/><div className="chat-start-tools"><button onClick={()=>setView('orders')}><Package size={15}/> Find an order</button><span/><button onClick={()=>setView('reserve')}><LockKeyhole size={15}/> Explore Reserve Pay</button></div></>}
    </div>
    {query&&<>{voice.supportOrder&&<section className="support-handoff panel"><h3>Review your support request</h3><p>Your assistant found the order. Review the case details before sending them to the merchant. No refund or cancellation has been executed.</p><button className="primary" onClick={()=>{setSupportOrder(voice.supportOrder!);setView('support')}}>Review order and contact merchant</button></section>}{(voice.reply!==null||voice.items.length>0)&&<section className="recommendation-story" key="store-results" aria-label="Copilot product recommendations"><header hidden={!shown.length}><span><span className="store-dot"/> The Green Basket edit</span><span>{`${shown.length} finds`} · live catalogue</span></header><ul className="product-runway" hidden={!shown.length} aria-label="Recommended products">{shown.map((p,i)=><li className="runway-item" key={p.id} style={{'--card-order':i} as React.CSSProperties}><ProductCard detailOpen={product?.id===p.id} product={p} quantity={basket[p.id]||0} disabled={!cartReady||cartBusy>0||paymentLocked||stage==='checking'||stage==='confirmed'} onRemove={source=>add(p.id,-1,source)} onView={()=>setProduct(p)} onAdd={source=>add(p.id,1,source)}/></li>)}</ul>{!shown.length&&(catalogueLoading||catalogueError)&&<Empty title={catalogueLoading?'Fetching the shelf…':catalogueError?'The store is unreachable.':'Let’s try another thought.'} description={catalogueLoading?'Asking the store what it has today.':catalogueError||'Ask for milk, fruit, coffee or another everyday essential.'}/>}<div className="basket-chat-updates" role="log" aria-live="polite" aria-label="Cart updates" aria-relevant="additions">{basketUpdates.map(update=><div key={update.id} className={update.delta>0?'basket-chat-added':'basket-chat-removed'}><span>{update.delta>0?<Plus size={13}/>:<Minus size={13}/>}</span><p><strong>{update.delta>0?'Added':'Removed'} {Math.abs(update.delta)} · {update.name}</strong><span>{update.quantity?`${update.quantity} in your basket`:'Removed from your basket'}</span></p></div>)}</div>{shown.length>1&&<ProductComparison products={shown}/>}<div hidden={!shown.length} className="recommendation-caption"><span>{beyond>0?`Picked for this conversation · ${beyond} more on the shelf, ask for anything.`:'Picked for this conversation.'}</span><span>Scroll to explore</span></div>{count>0&&<div className="conversation-basket" style={{viewTransitionName:reviewOpen?undefined:'order-surface'}}><div className="basket-peek-images">{lines.slice(0,3).map(p=><ProductArt key={p.id} product={p}/>)}</div><div><strong>{count} {count===1?'good find':'good finds'}</strong><span>{money(total)} · delivery included</span></div><button onClick={()=>transition(reviewOrder)}>Review order <ArrowRight size={17}/></button></div>}</section>}<div className="conversation-composer"><Composer onSend={send} phase={response.phase} onStop={response.stop}/><p>Connected conversation · Purchase approval stays with you.</p></div></>}
   </>}
   {view==='orders'&&<><LiveOrders initialOrderId={orderSnapshot?.orderId}/><details><summary>Illustrative resolution practice</summary><SimulatedOrders/></details></>}
   {view==='orders'&&<div className="tracking-composer-dock"><Composer onSend={send}/><small>Ask Razor AI about your order</small></div>}
   <div hidden={view!=='reserve'}><ReservePay/></div>
   {view==='support'&&<><LiveOrders key={supportOrder} initialOrderId={supportOrder??orderSnapshot?.orderId} support/><details><summary>Illustrative resolution practice</summary><SimulatedOrders support/></details></>}
   <footer className="workspace-foot"><span>Green Basket · sample store</span><span><ShieldCheck size={13}/> Your approval. Your control.</span><Link href="/merchant">Merchant workspace <ArrowUpRight size={13}/></Link></footer>
   </main>
   {cartOpen&&<><button className="cart-backdrop" aria-label="Close cart" onClick={()=>setCartOpen(false)}/><aside className="basket-rail"><div className="basket-header"><h2>Your cart <span>{count}</span></h2><button className="icon-button" aria-label="Close cart" onClick={()=>setCartOpen(false)}><X size={19}/></button></div>
    {stage==='confirmed'?<div className="basket-success"><span className="success-ring"><Check size={33}/></span><Badge tone="green">Demo order confirmed</Badge><h2>Good choice.<br/>Great morning.</h2><p>Your simulated purchase is complete. No real payment was made.</p><Primary onClick={()=>{setView('orders');setCartOpen(false)}}>View your order <ArrowRight size={16}/></Primary><button className="subtle" onClick={()=>setProof(true)}>View transaction proof</button><button className="subtle" onClick={()=>{startNewCart()}}>Start a new cart</button></div>:stage==='checking'?<div className="basket-success"><div className="checking-orbit"/><h2>Checking the payment.</h2><p>Waiting for simulated provider evidence. A browser callback alone never confirms a purchase.</p><Badge tone="amber">Please don’t pay again</Badge></div>:<>{!count?<Empty title="Room for something good." description="Your finds will live here. Add an essential to get started."/>:<><div className="basket-lines">{lines.map(p=><div className="basket-line" key={p.id}><div className="basket-product-art" style={{background:p.color}}><Image src={p.image} alt={p.name} unoptimized width={640} height={640}/></div><div className="basket-line-info"><strong>{p.name}</strong><span>{p.unit.split(' · ')[0]}</span><div className="quantity"><button aria-label={`Remove one ${p.name}`} disabled={!cartReady||cartBusy>0||paymentLocked} onClick={e=>add(p.id,-1,e.currentTarget.closest('.basket-line') as HTMLElement)}><Minus size={12}/></button><span>{basket[p.id]}</span><button aria-label={`Add one ${p.name}`} disabled={!cartReady||cartBusy>0||paymentLocked} onClick={e=>add(p.id,1,e.currentTarget.closest('.basket-line') as HTMLElement)}><Plus size={12}/></button></div></div><strong>{money(p.price*basket[p.id])}</strong></div>)}</div>{quote&&<div className="delivery-progress"><span>{quote.free_delivery_applied?'A little perk: delivery is on us.':quote.gap_to_free_delivery_minor>0?`${money(quote.gap_to_free_delivery_minor)} away from free delivery`:`Delivery: ${money(delivery)}`}</span>{(quote.free_delivery_applied||quote.gap_to_free_delivery_minor>0)&&<div><i style={{width:quote.free_delivery_applied?'100%':`${Math.min(100,100*subtotal/Math.max(1,subtotal+quote.gap_to_free_delivery_minor))}%`}}/></div>}</div>}<div className="basket-totals"><div><span>Item subtotal</span><span>{money(subtotal)}</span></div><div><span>Delivery</span><span>{delivery?money(delivery):'On us'}</span></div>{discount>0&&<div className="green-text"><span>{quote?.offer_label||'Merchant offer'}</span><span>−{money(discount)}</span></div>}<div><span>Tax</span><span>{quote?money(quote.items_tax_minor+quote.delivery_tax_minor):'—'}</span></div><div className="total-line"><strong>Total</strong><strong key={total} className="amount-update">{money(total)}</strong></div><p>Prices and totals from the merchant’s current quote.</p></div>
    <Primary className="wide" onClick={()=>transition(reviewOrder)}>Review order <ArrowRight size={17}/></Primary><div className="basket-assurance"><ShieldCheck size={14}/> No payment without your approval.</div></> }</>}
   </aside></>}
  </div>
  <OrderReview onNavigate={navigateShop} key={storedCart?.cart_id??'no-cart'} onRecoveredOrder={order=>{if(order.quote)confirmPurchase(order.order_id,{amount_minor:order.amount_minor,quote:order.quote})}} onFreshReview={freshReview} cartId={storedCart?.cart_id} cartBusy={cartBusy>0||!!cartError} onContinue={text=>{setReviewOpen(false);setCartOpen(false);startNewCart(()=>send(text))}} open={reviewOpen} onClose={()=>{setReviewOpen(false);if(stage==='confirmed')startNewCart()}} lines={lines} basket={basket} subtotal={subtotal} discount={discount} delivery={delivery} total={stage==='confirmed'&&orderSnapshot?orderSnapshot.total:total} stage={stage} onQuantity={(id,n,source)=>{add(id,n,source)}} onManualConfirmed={manualConfirmed} onReserveConfirmed={reserveConfirmed} onOrder={()=>{setReviewOpen(false);setCartOpen(false);setView('orders');if(stage==='confirmed')startNewCart()}} onPaymentLock={setPaymentLocked} onProof={()=>setProof(true)}/>
  <Dialog open={!!product} onOpenChange={v=>!v&&setProduct(null)}><DialogContent className="concept-dialog product-dialog"><DialogHeader><DialogTitle>{product?.name}</DialogTitle><DialogDescription>{product?.unit}</DialogDescription></DialogHeader>{product&&<><div className="product-detail-art" style={{background:product.color,viewTransitionName:`product-${product.id}`}}><ProductArt product={product}/></div><div className="detail-price"><strong>{money(product.price)}</strong><Badge tone="green">{product.stock} units in catalogue</Badge></div><ProductFacts key={product.sku} sku={product.sku}/><Primary disabled={product.isListed===false||product.isAvailable===false||product.stock<=0} onClick={()=>{if(add(product.id)){setProduct(null);setCartOpen(true)}}}><Plus size={16}/> Add to your cart</Primary></>}</DialogContent></Dialog>
  <Dialog open={proof} onOpenChange={setProof}><DialogContent className="concept-dialog proof-dialog"><DialogHeader><DialogTitle>Transaction proof</DialogTitle><DialogDescription>Evidence read from your recorded purchase.</DialogDescription></DialogHeader>{orderSnapshot?.orderId?<LiveOrders initialOrderId={orderSnapshot.orderId}/>:<p>Select a recorded order in Your orders to verify its transaction evidence.</p>}</DialogContent></Dialog>
  <Dialog open={!!question} onOpenChange={v=>!v&&setQuestion('')}><DialogContent className="concept-dialog"><DialogHeader><DialogTitle>Your RazorSharp workspace</DialogTitle><DialogDescription>{question}</DialogDescription></DialogHeader><Link className="secondary" href="/">Back to platform home <ArrowUpRight size={16}/></Link></DialogContent></Dialog>


 </div></SidebarInset></SidebarProvider>;
}
