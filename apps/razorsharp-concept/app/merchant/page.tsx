'use client';
import {merchantPrompts as skills} from '@/lib/merchant-prompts';
import {WorkspaceComposer} from '@/components/workspace-composer';
import Link from 'next/link';
import {MerchantDashboard} from '@/components/merchant-dashboard';
import {ThemeToggle} from '@/components/theme-toggle';
import '../workspace-system.css';
import { SimulatedOrders } from '@/components/simulated-orders';
import { ShoppingShowcase } from '@/components/shopping-showcase';
import { LiveMerchantInsights } from '@/components/live-merchant-insights';
import { LiveMerchantPolicy } from '@/components/live-merchant-policy';
import { LiveMerchant } from '@/components/live-merchant';
import { LiveMerchantOrders } from '@/components/live-merchant-orders';
import { CampaignPlacement } from '@/components/discovery-cards';
import { useMotionState } from '@/components/continuity';
import {
  VoiceSessionProvider,
  useVoiceSession,
} from '@/components/voice-session';
import { useState, useEffect, useRef } from 'react';
import { MERCHANT_STATE_CHANGED } from '@/lib/merchant-sync';

import {
  ArrowLeft,
  ArrowUpRight,
  LayoutDashboard,
  Package,
  ShoppingBag,
  Headphones,
  FileCheck2,
  Activity,
  Send,
  Sparkles,
  ChevronDown,
  TrendingUp,
  ShieldCheck,
  PanelRightClose,
  SlidersHorizontal,
  LockKeyhole,
  ChevronRight,
  Workflow,
  Store,
  Maximize2,
  Minimize2,
  Plus,
} from 'lucide-react';
import {
  SidebarProvider,
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarInset,
  SidebarTrigger,
} from '@/components/ui/sidebar';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Brand, Badge, Composer, SectionHeading } from '@/components/concept';
import { capabilities } from '@/lib/demo';
import {
  useMerchantFacts,
  topicOf,
} from '@/lib/merchant-facts';
import { useChatHistory } from '@/lib/use-chat-history';
import {
  type ResponsePhase,
  ResponseActivity,
} from '@/components/response-motion';

const nav = [
  ['overview', 'Dashboard', LayoutDashboard],
  ['growth', 'Business & growth', TrendingUp],
  ['catalogue', 'Catalogue & inventory', Package],
  ['orders', 'Orders', ShoppingBag],
  ['campaigns', 'Campaigns', Send],
  ['support', 'Customer Support Desk', Headphones],
  ['policy', 'Merchant Policy', FileCheck2],
  ['activity', 'Activity & proof', Activity],
  ['operations', 'Operations', Workflow],
] as const;

export default function Merchant() {
  return (
    <VoiceSessionProvider>
      <MerchantWorkspace />
    </VoiceSessionProvider>
  );
}
function MerchantWorkspace() {
  const [responsePhase, setResponsePhase] = useState<ResponsePhase>('idle');
  const response = {
    phase: responsePhase,
    start: () => setResponsePhase('thinking'),
    stop: () => setResponsePhase('stopped'),
    reset: () => setResponsePhase('idle'),
  };
  const voice = useVoiceSession();
  const goBack = () => {
    if (typeof window !== 'undefined' && window.history.length > 1) window.history.back();
    else window.location.assign('/');
  };
  const [merchantRevision, setMerchantRevision] = useState(0);
  const [view, setView] = useMotionState('overview');
  const [supportOrder, setSupportOrder] = useState<{
    id: string;
    reference: string;
  } | null>(null);
  const [copilot, setCopilot] = useState(false);
  const [dashboardOrder,setDashboardOrder]=useState<string|undefined>();
  // A conversation, not one slot. Every question used to overwrite the last, so a merchant
  // could not read back what they had already asked -- the buyer's copilot has kept a real
  // history all along, and this is the same hook it uses.
  const chat = useChatHistory('merchant');
  const turns = chat.active?.messages ?? [];
  const [wide, setWide] = useState(false);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const lastReply = turns[turns.length - 1]?.reply;
  useEffect(() => {
    const node = conversationRef.current;
    if (node) node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' });
  }, [turns.length, lastReply]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setCopilot((open) => !open);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      if (window.location.hash === '#support') setView('support');
    });
    return () => cancelAnimationFrame(frame);
  }, [setView]);
  const facts = useMerchantFacts(merchantRevision);
  useEffect(() => {
    const refresh = () => setMerchantRevision((value) => value + 1);
    window.addEventListener(MERCHANT_STATE_CHANGED, refresh);
    return () => window.removeEventListener(MERCHANT_STATE_CHANGED, refresh);
  }, []);
  const [notice, setNotice] = useState('');
  // The answer is saved with the turn rather than recomputed on every render: a chat log
  // that silently rewrites what it already said is not a log. It also means an older answer
  // keeps the figures that were true when it was given.
  const voiceTarget = useRef<{chatId:string;messageId:string}|null>(null);
  const previousVoiceReply = useRef<string|null>(null);
  useEffect(()=>{
    if(voice.reply===null||voice.reply===previousVoiceReply.current)return;
    previousVoiceReply.current=voice.reply;
    if(voiceTarget.current)chat.reply(voiceTarget.current,voice.reply);
    setMerchantRevision(value=>value+1);
    setResponsePhase('complete');
  },[voice.reply,chat.reply]);
  useEffect(()=>{if(voice.notice)setResponsePhase('complete')},[voice.notice]);
  const ask = (text: string) => {
    if(voice.phase==='transcribing'||voice.phase==='speaking')return;
    previousVoiceReply.current=null;
    voiceTarget.current=chat.append(text);
    response.start();
    setCopilot(true);
    const panel={campaign:'campaigns',stock:'catalogue',support:'support',pricing:'policy',sales:null}[topicOf(text)];
    if(panel)setView(panel);
    voice.say(text);
  };

  return (
    <SidebarProvider
      style={{ '--sidebar-width': '235px' } as React.CSSProperties}
      className="merchant-app"
    >
      <Sidebar className="merchant-sidebar">
        <SidebarHeader className="merchant-brand">
          <Brand />
          <span className="merchant-subbrand">MERCHANT COMMAND</span>
        </SidebarHeader>
        <SidebarContent>
          <div className="merchant-store">
            <span className="store-avatar">
              <Store size={18} />
            </span>
            <div>
              <strong>RazorSharp Store</strong>
              <span>Indiranagar, Bengaluru</span>
            </div>
            <ChevronDown size={13} />
          </div>
          <SidebarGroup>
            <SidebarGroupLabel>WORKSPACE</SidebarGroupLabel>
            <SidebarMenu>
              {nav.map(([id, label, Icon]) => (
                <SidebarMenuItem key={id}>
                  <SidebarMenuButton
                    isActive={view === id}
                    onClick={() => setView(id)}
                    className="merchant-nav-button" data-destination={id}
                  >
                    <Icon size={17} />
                    <span>{label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>FUTURE CHANNELS</SidebarGroupLabel>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  onClick={() =>
                    setNotice(
                      'Gmail API integration is planned. Email sending is not available yet.',
                    )
                  }
                >
                  <Send size={17} />
                  <span>Gmail</span>
                  <Badge>Later</Badge>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <div className="sidebar-trust">
            <ShieldCheck size={19} />
            <strong>Your business. Your say.</strong>
            <p>Every consequential change starts with your approval.</p>
          </div>
          <Link className="workspace-switch" href="/">
            <span className="avatar">VT</span>
            <div>
              <strong>Vedant Tyagi</strong>
              <span>Back to platform</span>
            </div>
            <ArrowUpRight size={16} />
          </Link>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset className="merchant-inset">
        <header className="merchant-topbar">
          <div>
            <button
              type="button"
              className="workspace-icon-button header-back-button"
              onClick={goBack}
              aria-label="Go back"
              title="Back"
            >
              <ArrowLeft size={17} />
            </button>
            <SidebarTrigger />
            <span className="breadcrumb">
              Workspace <ChevronRight size={13} />{' '}
              <b>{nav.find((n) => n[0] === view)?.[1]}</b>
            </span>
          </div>
          <div>
            <ThemeToggle />
            <Badge tone="amber">Connected demo workspace</Badge>
            <button
              className={`copilot-toggle ${copilot ? 'active' : ''}`}
              onClick={() => setCopilot(!copilot)}
              title="Toggle Copilot (⌘K / Ctrl+K)"
              aria-pressed={copilot}
            >
              <Sparkles size={16} /> Copilot
            </button>
            <button
              className="avatar"
              aria-label="Workspace profile"
              onClick={() =>
                setNotice(
                  'You are exploring a sample merchant session. Backend records are connected; practice records and placement previews are labelled separately.',
                )
              }
            >
              V
            </button>
          </div>
        </header>
        <div className={`merchant-body ${copilot ? 'copilot-visible' : ''}`}>
          <main className="merchant-main" key={view}>
            {view==='overview'&&<MerchantDashboard revision={merchantRevision} onNavigate={(destination,orderId)=>{setDashboardOrder(orderId);setView(destination)}} onAsk={ask}/>}
            {view === 'growth' && (
              <LiveMerchantInsights refreshKey={merchantRevision} />
            )}
            {view === 'catalogue' && (
              <LiveMerchant revision={merchantRevision} view="catalogue" />
            )}
            {view === 'orders' && (
              <>
                <LiveMerchantOrders initialOrderId={dashboardOrder}
                  onSupport={(order) => {
                    setSupportOrder(order);
                    setView('support');
                  }}
                />
                <details>
                  <summary>Practice with illustrative orders</summary>
                  <SimulatedOrders merchant />
                </details>
              </>
            )}
            {view === 'campaigns' && (
              <>
                <SectionHeading
                  eyebrow="STOREFRONT PREVIEW"
                  title="Storefront placements"
                  description="Preview a placement inside the shopping conversation. Email campaigns are not available."
                />
                <CampaignPlacement />
              </>
            )}
            {view === 'support' && (
              <>
                <LiveMerchant
                  key={supportOrder?.id || 'all-support'}
                  revision={merchantRevision}
                  view="support"
                  orderFilter={supportOrder}
                  onClearOrderFilter={() => setSupportOrder(null)}
                />
                <details>
                  <summary>Practice an illustrative support case</summary>
                  <SimulatedOrders merchant support />
                </details>
              </>
            )}
            {view === 'policy' && <LiveMerchantPolicy />}
            {view === 'activity' && (
              <LiveMerchant revision={merchantRevision} view="activity" />
            )}
            {view === 'operations' && (
              <>
                <SectionHeading
                  eyebrow="PLATFORM OPERATIONS"
                  title="Inspect the recorded state."
                  description="Tenant Safe Mode, reconciliation and execution evidence are available in the platform console."
                />
                <section className="panel">
                  <h3>Platform console</h3>
                  <p>
                    Merchant permissions do not operate Safe Mode. Open the
                    separate demo operator session to inspect real queue records
                    and use confirmed incident controls.
                  </p>
                  <Link className="secondary" href="/platform">
                    Open platform console <ArrowUpRight size={15} />
                  </Link>
                </section>
                <section className="panel">
                  <h3>Connected capabilities and previews</h3>
                  <p className="muted">
                    Voice works here too: press the microphone in the composer
                    below to talk through operations, or type — typed questions
                    go to your business copilot.
                  </p>
                  <div className="capability-list">
                    {capabilities.map(([a, b, c]) => (
                      <div key={a}>
                        <span>
                          <strong>{a}</strong>
                          <small>{b}</small>
                        </span>
                        <Badge>{c}</Badge>
                      </div>
                    ))}
                  </div>
                </section>
              </>
            )}

            <footer className="merchant-foot">
              Connected merchant workspace{' '}
              <span>
                Storefront and operations previews are labelled separately.
              </span>
            </footer>
          {!copilot&&<WorkspaceComposer step="merchant" onSend={text=>{setCopilot(true);ask(text)}}/>}
          </main>
          {copilot && (
            <aside className={`merchant-copilot ${wide ? 'is-wide' : ''}`}>
              <header>
                <div>
                  <strong>Your business copilot</strong>
                  <span>Context: {nav.find((n) => n[0] === view)?.[1]}</span>
                </div>
                <div className="copilot-header-actions">
                  {turns.length > 0 && (
                    <button
                      className="icon-button"
                      aria-label="Start a new conversation"
                      title="New conversation"
                      onClick={() => {
                        response.reset();
                        chat.open(null);
                      }}
                    >
                      <Plus size={16} />
                    </button>
                  )}
                  <button
                    className="icon-button"
                    aria-label={
                      wide ? 'Narrow the copilot' : 'Widen the copilot'
                    }
                    title={wide ? 'Narrow' : 'Widen'}
                    aria-pressed={wide}
                    onClick={() => setWide((w) => !w)}
                  >
                    {wide ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
                  </button>
                  <button
                    className="icon-button"
                    aria-label="Close copilot"
                    onClick={() => setCopilot(false)}
                  >
                    <PanelRightClose size={17} />
                  </button>
                </div>
              </header>
              <div className="copilot-conversation" ref={conversationRef}>
                {/* The showcase and the welcome are an empty state, not furniture: once there is a
        conversation they would push every answer below the fold on a 305px panel. */}
                {!turns.length && (
                  <>
                    <ShoppingShowcase merchant onSelect={ask} />
                    <div className="copilot-welcome">
                      <h3>
                        A second pair of eyes.
                        <br />A few steps ahead.
                      </h3>
                      <p>
                        Let’s turn what’s happening in your business into what
                        happens next.
                      </p>
                    </div>
                  </>
                )}
                {turns.map((turn, index) => {
                  const last = index === turns.length - 1;
                  // Waiting is decided by whether the answer has arrived, not by the animation clock.
                  // The clock declares itself finished at 2.5s and a real Operations turn takes nine to
                  // twenty -- so keying on it left the activity indicator gone and the bubble empty for
                  // the whole of the actual wait, which reads as a hang.
                  const waiting = last && !turn.reply;
                  return (
                    <div className="copilot-turn" key={turn.id}>
                      <div className="user-bubble">{turn.user}</div>
                      {waiting && (
                        <ResponseActivity
                          phase={response.phase}
                          merchant
                          live
                        />
                      )}
                      {turn.reply && (
                        <div className="copilot-reply">
                          <span className="agent-name">
                            {' '}
                            Merchant Copilot <Badge>Demo</Badge>
                          </span>
                          <p>{turn.reply}</p>
                          {turn.execution && (
                            <details>
                              <summary>Execution details</summary>
                              <p>
                                Specialist:{' '}
                                {turn.execution.specialist || 'Not reported'} ·
                                Runtime:{' '}
                                {turn.execution.runtime || 'Not reported'}
                              </p>
                              <p>
                                {turn.execution.serverAuthored === true
                                  ? 'Platform-authored response'
                                  : turn.execution.serverAuthored === false
                                    ? 'Model-path response'
                                    : 'Response source not reported'}
                                {turn.execution.fallback === true
                                  ? ' · Deterministic fallback reported'
                                  : ''}
                              </p>
                              {turn.execution.routingReason && (
                                <p>Routing: {turn.execution.routingReason}</p>
                              )}
                              <p>
                                Tools called:{' '}
                                {turn.execution.tools.length
                                  ? turn.execution.tools.join(', ')
                                  : 'None reported'}
                              </p>
                              {turn.execution.corrections.length > 0 && (
                                <>
                                  <p>Recorded checks/corrections:</p>
                                  <ul>
                                    {turn.execution.corrections.map((c, i) => (
                                      <li key={i}>{c}</li>
                                    ))}
                                  </ul>
                                </>
                              )}
                            </details>
                          )}
                          <div className="source-chip">
                            <FileCheck2 size={13} />{' '}
                            {facts.loading
                              ? 'Reading your records…'
                              : 'Grounded response · check current records before acting'}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
                {!turns.length && (
                  <div className="copilot-suggestions">
                    <span>PUT YOUR COPILOT TO WORK</span>
                    {skills.map(([s, q], i) => (
                      <button key={s} onClick={() => ask(q)}>
                        <span className="skill-icon">
                          {
                            [
                              <TrendingUp size={16} key="1" />,
                              <Send size={16} key="2" />,
                              <Package size={16} key="3" />,
                              <SlidersHorizontal size={16} key="4" />,
                              <Headphones size={16} key="5" />,
                            ][i]
                          }
                        </span>
                        {s}
                        <ArrowUpRight size={13} />
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="copilot-compose">
                <Composer
                  surface="merchant"
                  compact
                  phase={response.phase}
                  onStop={() => {
                    voice.interrupt();
                    response.stop();
                  }}
                  onSend={ask}
                  keepExpanded
                  placeholder="Ask about your catalogue, stock or operations…"
                />
                <p>
                  <LockKeyhole size={11} /> Proposes freely. Acts with your
                  approval.
                </p>
              </div>
            </aside>
          )}
        </div>
      </SidebarInset>
      <Dialog open={!!notice} onOpenChange={(v) => !v && setNotice('')}>
        <DialogContent className="concept-dialog">
          <DialogHeader>
            <DialogTitle>Workspace preview</DialogTitle>
            <DialogDescription>{notice}</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    </SidebarProvider>
  );
}
