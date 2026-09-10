'use client';
// Reserve Pay: one permission card answering "what may my copilot spend, and how much is
// left", then the purchases made under it.
//
// That shape came from a local-fixture preview (`reserve-lab.tsx`) which has since been
// deleted -- it ran a parallel Reserve Pay on a reducer, with invented purchases, invented
// capacity and an invented evidence trail, under the SAME class names as this screen. Two
// Reserve Pays on one page, one of them fictional, is a screenshot waiting to be mistaken
// for the real thing. This screen keeps its shape and drops every figure it could invent
// and this one cannot.
//
// WHAT THIS SCREEN WILL NOT DO
// ----------------------------
// * It does not split allocated capacity into "spent" and "pending". The preview drew two
//   bands because its reducer knew both. `GET /v1/reserve/authorities` reports capacity,
//   allocated and available -- allocated is HELD and SPENT together -- and there is no
//   route that breaks it down. One band, labelled for what it is.
// * It does not claim the purchase list is complete. Purchases are assembled from the
//   order list (see `reservePurchases`), so an admitted debit that has not become an order
//   is not in it. The section says so.
// * Validity is the backend's `expires_at`, not an illustrative "7 days".
import {useCallback,useEffect,useState} from 'react';
import {LockKeyhole,ShieldCheck,RefreshCw,ArrowRight,Check,Clock3,AlertTriangle} from 'lucide-react';
import {ReserveMotif} from './reserve-motif';
import {Badge,SectionHeading} from './concept';
import {Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription} from './ui/dialog';
import {Slider} from './ui/slider';
import {money} from '@/lib/demo';
import {type ReserveTerms,downloadReserveEvidence,downloadReserveTrustKeys} from '@/lib/reserve-evidence';
import {ReserveRequestError} from '@/lib/reserve-api';
import {commerce,permissions,reservePurchases,type Permission,type ReservePurchase} from '@/lib/reserve-api';

const PRESETS=[500,1000,2500,5000];
// Rupee ceilings for the two amounts. The backend's own bounds are wider
// (`per_purchase_limit_minor` le=500000, `capacity_minor` le=10000000, i.e. Rs 5,000 and
// Rs 1,00,000), so PURCHASE_MAX matches the backend exactly while CAPACITY_MAX is a
// narrowing this screen applies. A narrowing in the form is not enforcement -- a direct
// POST can still authorise more -- and the backend is the only place that could make it
// true. Not changed here because `routers/reserve.py` is owned by another task right now.
const PURCHASE_MAX=5000,CAPACITY_MAX=10000;

/**
 * One amount: a slider and a box that mean the same number.
 *
 * The box keeps its own text while it is being typed in. Committing every keystroke
 * straight to the slider would fight the person using it -- typing "1000" into the
 * capacity box passes through "1", which is below the minimum, and a control that clamps
 * per keystroke would snap them to the minimum before they reached the second digit. So
 * an in-range parse moves the slider immediately, anything else is left alone until blur,
 * and blur clamps whatever is there into range.
 */
function AmountField({id,label,value,min,max,step,onCommit,children}:{
 id:string;label:string;value:number;min:number;max:number;step:number;
 onCommit:(n:number)=>void;children?:React.ReactNode;
}){
 const [text,setText]=useState(String(value));
 // The slider and the presets are the other writers; when they move, the box follows.
 const [previousValue,setPreviousValue]=useState(value);if(previousValue!==value){setPreviousValue(value);setText(String(value))}
 const clamp=(n:number)=>Math.min(max,Math.max(min,Math.round(n)));
 return <div className="reserve-limit-control">
  <div><label id={id}>{label}</label><output>{money(value*100)}</output></div>
  <Slider aria-labelledby={id} min={min} max={max} step={step} value={[value]} onValueChange={v=>onCommit(clamp(Array.isArray(v)?v[0]:v))}/>
  <div className="reserve-slider-labels"><span>{money(min*100)}</span><span>{money(max*100)}</span></div>
  <label className="form-field reserve-amount-entry">Or type an amount (₹)
   <input type="number" inputMode="numeric" min={min} max={max} step={step} value={text}
    aria-describedby={id}
    onChange={e=>{const raw=e.target.value;setText(raw);const n=Number(raw);
     if(raw.trim()!==''&&Number.isFinite(n)&&n>=min&&n<=max)onCommit(Math.round(n))}}
    onBlur={()=>{const n=Number(text);const next=Number.isFinite(n)&&text.trim()!==''?clamp(n):value;setText(String(next));onCommit(next)}}/>
  </label>
  {children}
 </div>;
}

/** The permission a buyer is actually working under: the newest one still usable. */
function primaryOf(list:Permission[]):Permission|null{
 return list.find(a=>a.status==='ACTIVE'&&a.authorization_evidence?.status==='VERIFIED')??list[0]??null;
}

function expiry(iso:string):string{
 const at=new Date(iso);
 return Number.isNaN(at.getTime())?'unknown':at.toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'numeric'});
}

/** Where a purchase has reached. Read from the backend's own two words, never inferred. */
function stageOf(p:ReservePurchase):{icon:React.ReactNode;tone:'green'|'amber'|'neutral';done:number}{
 if(p.payment_status==='CAPTURED')return {icon:<Check/>,tone:'green',done:4};
 if(p.payment_status==='FAILED')return {icon:<AlertTriangle/>,tone:'amber',done:2};
 return {icon:<Clock3/>,tone:'neutral',done:3};
}

export function ReservePay(){
 const [approval,setApproval]=useState<{body:ReserveTerms;key:string}|null>(null);
 const [saved,setSaved]=useState<Permission[]>([]);
 const [purchases,setPurchases]=useState<ReservePurchase[]>([]);
 const [limit,setLimit]=useState(500),[budget,setBudget]=useState(2000);
 const [setup,setSetup]=useState(false),[revoking,setRevoking]=useState<Permission|null>(null);
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 const [loaded,setLoaded]=useState(false);

 const refresh=useCallback(async()=>{
  try{
   const [list,activity]=await Promise.all([permissions(),reservePurchases()]);
   setSaved(list.authorities);setPurchases(activity);
  }catch(e){setError((e as Error).message)}
  finally{setLoaded(true)}
 },[]);
 useEffect(()=>{const controller=new AbortController();const load=async()=>{try{const [list,activity]=await Promise.all([permissions(),reservePurchases()]);if(!controller.signal.aborted){setSaved(list.authorities);setPurchases(activity);setError('')}}catch(e){if(!controller.signal.aborted)setError((e as Error).message)}finally{if(!controller.signal.aborted)setLoaded(true)}};void load();return()=>controller.abort()},[]);

 const primary=primaryOf(saved);
 const usable=primary?.status==='ACTIVE'&&primary.authorization_evidence?.status==='VERIFIED';
 // Percentages off the backend's own integers. Nothing here derives an amount: the only
 // arithmetic is turning two paise figures into a bar width.
 const allocatedPct=primary&&primary.capacity_minor>0?Math.min(100,primary.allocated_minor/primary.capacity_minor*100):0;

 async function authorise(){
  setBusy(true);try{
   const intent=approval??{body:{per_purchase_limit_minor:limit*100,capacity_minor:budget*100},key:crypto.randomUUID()};setApproval(intent);
   await commerce('reserve/authorities','POST',intent.body,intent.key);
   setApproval(null);
   setSetup(false);await refresh();
  }catch(e){if(e instanceof ReserveRequestError&&e.status>=400&&e.status<500)setApproval(null);setError((e as Error).message)}finally{setBusy(false)}
 }
 async function revoke(id:string){
  setBusy(true);try{await commerce(`reserve/authorities/${id}/revoke`,'POST');setRevoking(null);await refresh()}
  catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }

 return <div className="reserve-page reserve-workspace">
  <div className="reserve-india-heading">
   <span className="reserve-native-note" lang="hi">आपकी मर्ज़ी। आपकी सीमा।</span>
   <SectionHeading eyebrow="RAZORSHARP / RESERVE PAY" title="A little freedom. Firm boundaries." description="Your copilot handles the details. You set the amounts, and the backend enforces them on every purchase." action={<Badge tone="amber">Simulated provider</Badge>}/>
  </div>

  {error&&<div className="review-changed" role="alert">{error}<button className="subtle" onClick={()=>void refresh()}>Try connection again</button></div>}

  <div className="reserve-command">
   <section className="reserve-permission">
    <ReserveMotif/>
    <div className="reserve-card-top">
     <span><LockKeyhole size={16}/> YOUR SPENDING PERMISSION</span>
     <Badge tone={primary?.status==='ACTIVE'?'green':primary?'amber':'neutral'}>{primary?primary.status:'Not authorised'}</Badge>
    </div>
    <h2>Everyday essentials.<br/><em>Handled with care.</em></h2>
    <p>{primary?'Covers every product this shop sells':'No permission set. Your copilot cannot spend anything.'}</p>
    <div className="reserve-budget">
     <strong>{money(primary?primary.available_minor:0)}</strong>
     <span>{usable?'available within permission':primary?'unused capacity · permission inactive':'nothing authorised'}</span>
    </div>
    {/* One band, not two. See the note at the top of this file. */}
    <div className="reserve-capacity"  aria-label={primary?`${money(primary.allocated_minor)} allocated or spent, ${money(primary.available_minor)} available of ${money(primary.capacity_minor)}`:'No permission'}>
     <i style={{width:allocatedPct+'%'}}/>
    </div>
    <div className="reserve-ledger">
     <span><i/>{money(primary?primary.allocated_minor:0)} allocated or spent</span>
     <span>{money(primary?primary.available_minor:0)} {usable?'available':'unused'}</span>
     <span>{money(primary?primary.capacity_minor:0)} total capacity</span>
    </div>
    <div className="reserve-terms">
     <span>Per purchase<strong>{money(primary?primary.per_purchase_limit_minor:0)}</strong></span>
     <span>Validity<strong>{primary?primary.status==='REVOKED'?'Revoked':primary.expires_at?expiry(primary.expires_at):'Until revoked':'—'}</strong></span>
     <span>Authority epoch<strong>{primary?primary.epoch:'—'}</strong></span>
    </div>
    {primary&&<output>{primary.authorization_evidence?.status==='VERIFIED'?'ES256 signature verified · simulator authorization':'This permission needs fresh authorization before it can be used.'}</output>}
    {primary?.authorization_evidence?.payload_sha256&&<details><summary>Authorization evidence</summary><p>Reserve provider simulator · ES256</p><p>{primary.authorization_evidence.provider_reference}</p><code style={{overflowWrap:'anywhere'}}>{primary.authorization_evidence.payload_sha256}</code><button className="subtle" onClick={()=>void downloadReserveEvidence(primary.authority_id).catch(e=>setError(e.message))}>Download signed proof</button><button className="subtle" onClick={()=>void downloadReserveTrustKeys().catch(e=>setError(e.message))}>Download public verification keys</button><p>Use the independent verifier with keys you trust. Offline checks cannot establish current revocation or remaining capacity.</p><p>Signature proves the saved bounds. Revocation and remaining capacity are checked again for every payment.</p></details>}
    <small>Enforced by the backend on every debit, under the authority&apos;s own row lock. NPCI UAP and your bank are not connected.</small>
    <div className="reserve-buttons">
     <button className="primary" onClick={()=>setSetup(true)}>{primary?'Set up another permission':'Set up permission'} <ArrowRight size={16}/></button>
     {primary&&primary.status!=='REVOKED'&&<button className="subtle" onClick={()=>setRevoking(primary)}>Revoke</button>}
    </div>
   </section>

   <section className="reserve-intent">
    <div className="reserve-intent-head"><ShieldCheck size={18}/><span>WHAT STILL GETS CHECKED</span></div>
    <div className="reserve-quiet-mark"><LockKeyhole size={28}/><span>LIMITED BY YOU.<br/>REVIEWABLE AT EVERY STEP.</span></div>
    <h3>Every purchase,<br/>checked again.</h3>
    <p>A saved permission is not a blank cheque. Each debit is decided under the authority&apos;s row lock, and any one of these refuses it.</p>
    <ol className="reserve-events">
     {[['Exact bill','the amount on the approved version, not a preview'],['Per-purchase limit','a bill over your limit is refused'],['Remaining capacity','allocations cannot exceed what you authorised'],['Revocation epoch','a revoked permission stops future spending'],['Until revoked','new permissions have no time-based expiry']].map(([k,v])=>
      <li key={k}><span>{k.toUpperCase()}</span><p>{v}</p></li>)}
    </ol>
   </section>
  </div>

  <section className="reserve-purchases">
   <div className="reserve-section-head">
    <div><span>PURCHASE ACTIVITY</span><h3>Paid with Reserve Pay</h3></div>
    <button className="subtle" onClick={()=>void refresh()} aria-label="Refresh"><RefreshCw size={14}/> Refresh</button>
   </div>
   {!loaded?<div className="reserve-empty"><Clock3 size={25}/><p>Reading your purchases…</p></div>
    :!purchases.length?<div className="reserve-empty"><ShieldCheck size={25}/><p>No Reserve purchases yet. Set a permission, then choose Reserve Pay at checkout.</p></div>
    :purchases.map(p=>{const stage=stageOf(p);return <article key={p.order_id} className={`reserve-purchase state-${p.payment_status.toLowerCase()}`}>
     <div className="reserve-purchase-heading">
      <span className="reserve-purchase-icon">{stage.icon}</span>
      <div><strong>{p.reference}</strong><span>{new Date(p.created_at).toLocaleString('en-IN')}</span></div>
      <strong>{money(p.amount_minor)}</strong>
      <Badge tone={stage.tone}>{p.payment_status}</Badge>
     </div>
     <output>Capacity {p.allocation === 'SPENT' ? 'spent' : p.allocation.toLowerCase()} · order {p.state.toLowerCase().replace(/_/g,' ')}</output>
     <div className="reserve-progress">
      {['Bill approved','Permission checked','Debit queued','Provider evidence'].map((label,i)=>
       <span className={i<stage.done?'complete':''} key={label}><i/>{label}</span>)}
     </div>
    </article>})}
   <small>Assembled from your order list, so a debit that has not become an order yet is not shown here.</small>
  </section>

  {saved.length>1&&<section className="panel">
   <h2>All saved permissions</h2>
   {saved.map(a=><article className="reserve-permission-mini" key={a.authority_id}>
    <LockKeyhole size={22}/>
    <div>
     <strong>{a.status} · {money(a.per_purchase_limit_minor)} per purchase</strong>
     <p>{money(a.available_minor)} available · {money(a.allocated_minor)} allocated or spent</p>
     <small>{a.expires_at?`Valid until ${expiry(a.expires_at)}`:'Valid until revoked'} · epoch {a.epoch}</small>
     {a.status!=='REVOKED'&&<button className="secondary" disabled={busy} onClick={()=>setRevoking(a)}>Revoke future spending</button>}
    </div>
   </article>)}
  </section>}

  <Dialog open={setup} onOpenChange={v=>{if(!busy)setSetup(v)}}>
   <DialogContent className="concept-dialog reserve-india-dialog">
    <DialogHeader>
     <DialogTitle>Your permission. Precisely defined.</DialogTitle>
     <DialogDescription>Saved on the backend and enforced there. No UPI mandate or bank block is created; the provider is simulated.</DialogDescription>
    </DialogHeader>
    <fieldset disabled={busy||!!approval} style={{border:0,padding:0,margin:0,minWidth:0}}>
    <AmountField id="reserve-limit-label" label="Maximum per purchase" value={limit}
     min={1} max={PURCHASE_MAX} step={1}
     onCommit={n=>{setLimit(n);setBudget(b=>Math.max(b,n))}}>
     <div className="reserve-limit-presets">{PRESETS.map(v=><button key={v} aria-pressed={limit===v} onClick={()=>{setLimit(v);setBudget(b=>Math.max(b,v))}}>₹{v.toLocaleString('en-IN')}</button>)}</div>
    </AmountField>
    {/* Lower end tracks the per-purchase limit, so neither control can reach a pair the
        backend refuses with "Capacity must cover the per-purchase limit". */}
    <AmountField id="reserve-capacity-label" label="Total authorised capacity" value={budget}
     min={limit} max={CAPACITY_MAX} step={100} onCommit={v=>{if(!busy&&!approval)setBudget(v)}}/>
    <div className="form-summary">
     <span>Covers</span><strong>Every product this shop sells</strong>
     <span>Validity</span><strong>Until you revoke it</strong>
     <span>Provider</span><strong>Simulated</strong>
    </div>
    </fieldset>
    <p>Confirm these limits to authorize simulated purchases from this merchant until you revoke permission. No real funds are blocked or debited.</p>
    <button className="primary" disabled={busy||budget<limit} onClick={()=>void authorise()}>{busy?'Saving permission…':approval?'Retry saving this approval':'Authorize simulated Reserve Pay'} <LockKeyhole size={15}/></button>
   </DialogContent>
  </Dialog>

  <Dialog open={!!revoking} onOpenChange={v=>!v&&setRevoking(null)}>
   <DialogContent className="concept-dialog reserve-india-dialog">
    <DialogHeader>
     <DialogTitle>Revoke future spending?</DialogTitle>
     <DialogDescription>This raises the authority&apos;s epoch, so nothing new can be admitted against it. A debit already submitted keeps its allocation until the provider outcome is known — revoking does not undo a payment in flight.</DialogDescription>
    </DialogHeader>
    <button className="primary" disabled={busy} onClick={()=>revoking&&void revoke(revoking.authority_id)}>{busy?'Revoking…':'Confirm revocation'}</button>
    <button className="secondary" onClick={()=>setRevoking(null)}>Keep permission active</button>
   </DialogContent>
  </Dialog>
 </div>;
}
