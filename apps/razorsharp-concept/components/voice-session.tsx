'use client';
// One voice conversation, over the real gateway.
//
// This used to be thirty-eight lines of `setTimeout` that typed out a hardcoded sentence
// and pretended to speak it. The gateway was finished and tested -- Gemini recognition, an
// echo gate, Chirp synthesis, twenty wire frames, eight live-audio tests -- and nothing in
// the browser had ever opened a socket to it. The shape below is the same shape the screens
// already consumed; what changed is that it is now connected to something.
//
// Two properties the surface must not lose:
//
//   * **Speech is never authority.** `session_ready` says `voice_is_authority: false`, and
//     that is honoured by omission here: this provider shows transcripts and plays audio,
//     and there is no path from a spoken word to an approval. Consent is the trusted
//     screen's, and a spoken yes is reported to the server, which decides what it meant.
//   * **Typing always works.** Recognition can be unconfigured, degraded or refused
//     permission, and each of those leaves the composer working and says so (19.12). The
//     failure that must never happen is a surface that goes quiet.

import {rawCommerceCall} from '@/lib/commerce';
import {projectTurn} from '@/lib/agent-turn';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { VoiceClient, type VoiceItem, type VoiceOffer } from '@/lib/voice/client';

type VoicePhase = 'idle' | 'listening' | 'transcribing' | 'ready' | 'speaking';

type VoiceSession = {
  phase: VoicePhase;
  transcript: string;
  finalTurn: {text: string; sequence: number} | null;
  reply: string | null;
  speech: string;
  spokenWords: number;
  /** Null while nothing is wrong. A sentence when recognition is unavailable or degraded. */
  notice: string | null;
  /** False until a socket is open, so a surface can offer typing without promising speech. */
  live: boolean;
  /** The products the last reply put on the page. The shelf redraws from this. */
  items: VoiceItem[];
  /**
   * The product the buyer asked to be added, once, for the surface to act on.
   *
   * Only ever set from an offer the server marked as a proposal, so looking at a product
   * does not fill a basket. The surface clears it with `takeProposal` after acting, which
   * is what stops a re-render adding it twice.
   */
  proposal: VoiceOffer | null;
  takeProposal: () => void;
  /**
   * Send a typed turn to the assistant over the open socket.
   *
   * Returns false when there is no socket, so the caller can fall back rather than
   * silently dropping what the buyer typed. Typing is the input that must never stop
   * working -- including when the microphone was declined, which no longer closes the
   * session.
   */
  say: (text: string) => boolean;
  /** Open the session without waiting to be spoken to. For a typed conversation. */
  connect: () => void;
  startListening: (merchant?: boolean) => void;
  finishListening: () => void;
  speak: (text: string) => void;
  checkoutGuidance: (checkoutId:string|null,stage?:string,version?:number)=>void;
  reset: () => void;
  interrupt: () => void;
};

const VoiceContext = createContext<VoiceSession | null>(null);

export function VoiceSessionProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [transcript, setTranscript] = useState('');
  const [finalTurn, setFinalTurn] = useState<{text: string; sequence: number} | null>(null);
  const [reply, setReply] = useState<string | null>(null);
  const [speech, setSpeech] = useState('');
  const [spokenWords, setSpokenWords] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [items, setItems] = useState<VoiceItem[]>([]);
  const [proposal, setProposal] = useState<VoiceOffer | null>(null);

  const client = useRef<VoiceClient | null>(null);
  const textGeneration = useRef(0);
  const connectionGeneration = useRef(0);
  const opening = useRef<Promise<void> | null>(null);
  // Word-by-word highlighting while the reply is spoken. Presentation only: the words are
  // the ones the server already sent as text, and text exists before speech by contract.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);

  useEffect(
    () => () => {
      timers.current.forEach(clearTimeout);
      connectionGeneration.current++;
      void client.current?.close();
    },
    [],
  );

  const reveal = useCallback(
    (text: string) => {
      clearTimers();
      setSpeech(text);
      setSpokenWords(0);
      setPhase('speaking');
      const words = text.split(' ');
      timers.current = words.map((_, i) => setTimeout(() => setSpokenWords(i + 1), i * 185));
    },
    [clearTimers],
  );

  const connect = useCallback(async () => {
    if (client.current) return;
    if (opening.current) return opening.current;
    const generation=++connectionGeneration.current;
    const created = new VoiceClient({
      onReady: () => {
        if(generation!==connectionGeneration.current){void created.close();return;}
        setLive(true);
        setNotice(null);
        setPhase('listening');
      },
      onPartial: (text) => {
        if (text) setTranscript(text);
        setPhase('listening');
      },
      onFinal: (text, stale) => {
        textGeneration.current++;
        if (text && !stale) {
          setFinalTurn(previous => ({text, sequence: (previous?.sequence ?? 0) + 1}));
          setReply(null);
          setItems([]);
        }
        if (text) setTranscript(text);
        setPhase(stale ? 'idle' : 'transcribing');
        if (stale)
          setNotice('That took too long to reach the assistant. Say it again, or type it.');
      },
      onReply: (text) => { setReply(text); reveal(text); },
      onItems: (next) => setItems(next),
      // Held, not applied. This provider does not touch a basket; the shop reads the
      // proposal, adds it on the trusted surface and clears it.
      onOffer: (offer) => setProposal(offer.isProposal ? offer : null),
      onSpeaking: (speaking) => {
        if (!speaking) {
          clearTimers();
          setSpeech('');
          setSpokenWords(0);
          setTranscript('');
          // Playback ending is a turn boundary, not the end of the conversation.
          setPhase(created.listening ? 'listening' : 'idle');
        }
      },
      onDegraded: (_kind, message) => setNotice(message || 'Speech is degraded. Typing still works.'),
      // Not an error: the buyer declined a permission and the rest of the session works.
      onMicUnavailable: (message) => {
        setNotice(message);
        setPhase('idle');
      },
      onError: (message) => setNotice(message),
      onClosed: (reason) => {
        setLive(false);
        setPhase('idle');
        setNotice(`The voice conversation ended: ${reason}. Typing still works.`);
        client.current = null;
      },
    });
    opening.current = created
      .open()
      .then(() => {
        if(generation!==connectionGeneration.current){void created.close();return;}
        client.current = created;
      })
      .catch((cause: unknown) => {
        if(generation!==connectionGeneration.current)return;
        // Every reason a microphone or a gateway can refuse ends here, and all of them
        // leave the composer working. A surface that just stopped responding would be the
        // one failure the specification's degradation rules exist to prevent.
        setLive(false);
        setPhase('idle');
        setNotice(
          cause instanceof DOMException && cause.name === 'NotAllowedError'
            ? 'Microphone access was declined, so I cannot listen. Type instead.'
            : (cause as Error)?.message || 'Voice is unavailable right now. Type instead.',
        );
      })
      .finally(() => {
        opening.current = null;
      });
    return opening.current;
  }, [clearTimers, reveal]);

  const startListening = useCallback(() => {
    setTranscript('');
    setSpeech('');
    setSpokenWords(0);
    setNotice(null);
    setPhase('listening');
    void connect();
  }, [connect]);

  const finishListening = useCallback(() => {
    connectionGeneration.current++;
    clearTimers();setSpeech('');setSpokenWords(0);
    // Explicit Finish stops capture. Finishing a spoken reply does not call this.
    const active=client.current;client.current=null;
    void active?.close();setLive(false);setPhase('idle');setTranscript('');
  }, [clearTimers]);

  const speak = useCallback(
    (text: string) => {
      // Local rendering of a sentence the surface already has. Nothing is sent to the
      // gateway: speaking is the server's to do, from text it produced.
      reveal(text);
      timers.current.push(
        setTimeout(
          () => {
            setPhase('idle');
            setSpeech('');
          },
          text.split(' ').length * 185 + 450,
        ),
      );
    },
    [reveal],
  );

  const interrupt = useCallback(() => {
    client.current?.bargeIn();
    clearTimers();setSpeech('');setSpokenWords(0);setTranscript('');
    setPhase(client.current?.listening ? 'listening' : 'idle');
  }, [clearTimers]);

  const reset = useCallback(() => {
    connectionGeneration.current++;
    const active=client.current;client.current=null;
    void active?.close();setLive(false);
    textGeneration.current++;
    clearTimers();
    setPhase('idle');
    setTranscript('');
    setSpeech('');
    setSpokenWords(0);
    setNotice(null);
    setItems([]);
    setReply(null);
    setProposal(null);
  }, [clearTimers]);

  const takeProposal = useCallback(() => setProposal(null), []);

  const say = useCallback(
    (text: string) => {
      const generation = ++textGeneration.current;
      setTranscript(text);
      setReply(null);
      setItems([]);
      setPhase('transcribing');
      if (client.current) {
        // Typing over a reply is barging in. It has the same meaning as talking over it --
        // the buyer has moved on -- so it takes the same path: the client flushes its own
        // queued audio first, then tells the server, which bumps the speech generation and
        // closes any consent window that was open on what they had not finished hearing.
        // Without this the old answer keeps playing while the new one is being reasoned
        // about, and the buyer hears two conversations at once.
        clearTimers();
        setSpeech('');
        setSpokenWords(0);
        client.current.bargeIn();
        client.current.text(text);
        return true;
      }
      // With no connected voice session, typing uses HTTP directly. Never retry a
      // socket turn here: its outcome may be unknown and replay could duplicate intent.
      setNotice(null);
      void rawCommerceCall<{reply:string;structured:unknown}>('agent/turn', {
        method:'POST',body:{message:text},idempotencyKey:crypto.randomUUID(),
      }).then(result=>{
        if(generation!==textGeneration.current)return;
        const projected=projectTurn(result.structured);
        setReply(result.reply);setItems(projected.items);setProposal(projected.proposal);setPhase('idle');
      }).catch(error=>{if(generation===textGeneration.current){setNotice(error.message);setPhase('idle')}});
      return true;
    },
    [clearTimers, connect],
  );

  const value = useMemo(
    () => ({
      phase,
      transcript,
      finalTurn,
      reply,
      speech,
      spokenWords,
      notice,
      live,
      items,
      proposal,
      takeProposal,
      say,
      connect: () => void connect(),
      startListening,
      finishListening,
      speak,
      checkoutGuidance: (id:string|null,stage?:string,version?:number)=>client.current?.checkoutGuidance(id,stage,version),
      reset,
      interrupt,
    }),
    [phase, transcript, finalTurn, reply, speech, spokenWords, notice, live, items, proposal, takeProposal, say, connect, startListening, finishListening, speak, reset, interrupt],
  );

  return <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>;
}

export function useVoiceSession() {
  const context = useContext(VoiceContext);
  if (!context) throw Error('Voice session requires VoiceSessionProvider');
  return context;
}
