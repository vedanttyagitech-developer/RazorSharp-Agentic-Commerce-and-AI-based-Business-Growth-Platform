'use client';
import {useLayoutEffect} from 'react';
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

import {conversationPhase} from '@/lib/voice/conversation';
import { rawCommerceCall } from '@/lib/commerce';
import { projectTurn } from '@/lib/agent-turn';
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

import {
  VoiceClient,
  type VoiceItem,
  type VoiceOffer,
} from '@/lib/voice/client';

type VoicePhase = 'idle' | 'listening' | 'transcribing' | 'ready' | 'speaking';

type VoiceSession = {
  phase: VoicePhase;
  transcript: string;
  finalTurn: { text: string; sequence: number } | null;
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
  cartUpdated: (cartId: string, eventId: string) => void;
  /** Open the session without waiting to be spoken to. For a typed conversation. */
  connect: () => void;
  startListening: (merchant?: boolean) => void;
  finishListening: () => void;
  speak: (text: string) => void;
  reportCartResult: (text: string) => void;
  checkoutGuidance: (
    checkoutId: string | null | undefined,
    stage?: string,
    version?: number,
  ) => void;
  reset: () => void;
  interrupt: () => void;
};

const VoiceContext = createContext<VoiceSession | null>(null);

export function VoiceSessionProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [transcript, setTranscript] = useState('');
  const [finalTurn, setFinalTurn] = useState<{
    text: string;
    sequence: number;
  } | null>(null);
  const preserveCartShelf = useRef(false);
  const [reply, setReply] = useState<string | null>(null);
  const [speech, setSpeech] = useState('');
  const [spokenWords, setSpokenWords] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [items, setItems] = useState<VoiceItem[]>([]);
  const [proposal, setProposal] = useState<VoiceOffer | null>(null);

  const client = useRef<VoiceClient | null>(null);
  const pendingClient = useRef<VoiceClient | null>(null);
  const checkoutContext = useRef<[string | null | undefined, string | undefined, number | undefined] | null>(null);
  const textGeneration = useRef(0);
  const connectionGeneration = useRef(0);
  const opening = useRef<Promise<void> | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttempts = useRef(0);
  const voiceWanted = useRef(false);
  const connectLatest = useRef<() => Promise<void>>(async () => {});
  const stopReconnect = useCallback(() => {
    voiceWanted.current = false;
    reconnectAttempts.current = 0;
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    reconnectTimer.current = null;
  }, []);
  const scheduleReconnect = useCallback(() => {
    if (!voiceWanted.current || reconnectTimer.current) return;
    if (reconnectAttempts.current >= 3) {
      setNotice('Voice could not reconnect. Tap the microphone to try again; typing still works.');
      return;
    }
    const delay = [1000, 2000, 4000][reconnectAttempts.current++];
    setNotice('Voice disconnected. Reconnecting; previous requests will not be repeated.');
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null;
      if (voiceWanted.current) void connectLatest.current();
    }, delay);
  }, []);
  // Word-by-word highlighting while the reply is spoken. Presentation only: the words are
  // the ones the server already sent as text, and text exists before speech by contract.
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);

  useEffect(
    () => () => {
      stopReconnect();
      timers.current.forEach(clearTimeout);
      connectionGeneration.current++;
      void client.current?.close();
      void pendingClient.current?.close();
    },
    [stopReconnect],
  );

  const reveal = useCallback(
    (text: string) => {
      clearTimers();
      setSpeech(text);
      setSpokenWords(0);
      // Text is available now; audible playback is reported by the client reducer.
    },
    [clearTimers],
  );

  const connect = useCallback(async () => {
    voiceWanted.current = true;
    if (client.current) return;
    if (opening.current) return opening.current;
    const generation = ++connectionGeneration.current;
    const connection: {client?:VoiceClient} = {};
    const created = new VoiceClient({
      onConversation: (state) => {
        if(generation!==connectionGeneration.current)return;
        setPhase(conversationPhase(state));
      },
      onReady: () => {
        if (generation !== connectionGeneration.current) {
          void connection.client?.close();
          return;
        }
        setLive(true);
        setNotice(null);
        // Restore read-only checkout guidance, never a user command or approval.
        if (checkoutContext.current) connection.client?.checkoutGuidance(...checkoutContext.current);
      },
      onPartial: (text) => {
        if (generation !== connectionGeneration.current) return;
        if (text) setTranscript(text);
      },
      onFinal: (text, stale) => {
        if (generation !== connectionGeneration.current) return;
        textGeneration.current++;
        if (text && !stale) {
          preserveCartShelf.current = false;
          setFinalTurn((previous) => ({
            text,
            sequence: (previous?.sequence ?? 0) + 1,
          }));
          setReply(null);
          setProposal(null);
        }
        if (text) setTranscript(text);
        if (stale)
          setNotice(
            'That took too long to reach the assistant. Say it again, or type it.',
          );
      },
      onReply: (text) => {
        if (generation !== connectionGeneration.current) return;
        setReply(text);
        reveal(text);
      },
      onAudioStarted: () => {
        if (generation === connectionGeneration.current) setSpokenWords(0);
      },
      onItems: (next) => {
        if (generation === connectionGeneration.current && !preserveCartShelf.current) setItems(next);
      },
      // Held, not applied. This provider does not touch a basket; the shop reads the
      // proposal, adds it on the trusted surface and clears it.
      onOffer: (offer) => {
        if (generation === connectionGeneration.current)
          setProposal(offer.isProposal ? offer : null);
      },
      onSpeaking: (speaking) => {
        if (generation !== connectionGeneration.current) return;
        if (!speaking) {
          clearTimers();
          setSpeech('');
          setSpokenWords(0);
          setTranscript('');
          // Playback ending is a turn boundary, not the end of the conversation.
        }
      },
      onDegraded: (kind, message) => {
        if (generation !== connectionGeneration.current) return;
        setNotice(message || 'Speech is degraded. Typing still works.');
        if (
          [
            'reasoning_failed',
            'stale_turn_dropped',
            'card_unavailable',
          ].includes(kind)
        ) {
          clearTimers();
          setSpeech('');
          setSpokenWords(0);
        }
      },
      // Not an error: the buyer declined a permission and the rest of the session works.
      onMicUnavailable: (message) => {
        if (generation !== connectionGeneration.current) return;
        setNotice(message);
      },
      onError: (message) => {
        if (generation !== connectionGeneration.current) return;
        setNotice(message);
        clearTimers();
        setSpeech('');
        setSpokenWords(0);
      },
      onClosed: (reason, retryable) => {
        if (generation !== connectionGeneration.current) return;
        setLive(false);
        setNotice(
          `The voice conversation ended: ${reason}. Typing still works.`,
        );
        client.current = null;
        if (retryable) scheduleReconnect();
        else stopReconnect();
      },
    });
    connection.client = created;
    pendingClient.current = created;
    opening.current = created
      .open()
      .then(() => {
        if (generation !== connectionGeneration.current) {
          void created.close();
          return;
        }
        client.current = created;
      })
      .catch((cause: unknown) => {
        if (generation !== connectionGeneration.current) return;
        // Every reason a microphone or a gateway can refuse ends here, and all of them
        // leave the composer working. A surface that just stopped responding would be the
        // one failure the specification's degradation rules exist to prevent.
        setLive(false);
        setPhase('idle');
        setNotice(
          cause instanceof DOMException && cause.name === 'NotAllowedError'
            ? 'Microphone access was declined, so I cannot listen. Type instead.'
            : (cause as Error)?.message ||
                'Voice is unavailable right now. Type instead.',
        );
      })
      .finally(() => {
        if (pendingClient.current === created) pendingClient.current = null;
        if (generation === connectionGeneration.current) {
          opening.current = null;
          if (!client.current && voiceWanted.current && reconnectAttempts.current > 0) scheduleReconnect();
        }
      });
    return opening.current;
  }, [clearTimers, reveal, scheduleReconnect, stopReconnect]);
  useLayoutEffect(() => {connectLatest.current = connect;});

  const startListening = useCallback(() => {
    reconnectAttempts.current = 0;
    void connect();
  }, [connect]);

  const finishListening = useCallback(() => {
    stopReconnect();
    connectionGeneration.current++;
    opening.current = null;
    void pendingClient.current?.close();
    pendingClient.current = null;
    clearTimers();
    setSpeech('');
    setSpokenWords(0);
    // Explicit Finish stops capture. Finishing a spoken reply does not call this.
    const active = client.current;
    client.current = null;
    void active?.close();
    setLive(false);
    setPhase('idle');
    setTranscript('');
  }, [clearTimers,stopReconnect]);

  const speak = useCallback((text: string) => {
    // Text guidance only. Never label a silent animation as audible speech.
    clearTimers();
    setSpeech(text);
    setSpokenWords(text.split(' ').length);
    setPhase('idle');
  }, [clearTimers]);

  const reportCartResult = useCallback((text:string) => {setReply(text);speak(text)}, [speak]);

  const interrupt = useCallback(() => {
    client.current?.bargeIn();
    clearTimers();
    setSpeech('');
    setSpokenWords(0);
    setTranscript('');
    setPhase(client.current?.listening ? 'listening' : 'idle');
  }, [clearTimers]);

  const reset = useCallback(() => {
    stopReconnect();
    checkoutContext.current = null;
    connectionGeneration.current++;
    opening.current = null;
    void pendingClient.current?.close();
    pendingClient.current = null;
    const active = client.current;
    client.current = null;
    void active?.close();
    setLive(false);
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
  }, [clearTimers,stopReconnect]);

  const takeProposal = useCallback(() => setProposal(null), []);

  const say = useCallback(
    (text: string) => {
      preserveCartShelf.current = false;
      const generation = ++textGeneration.current;
      setTranscript(text);
      setReply(null);
      setProposal(null);
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
        if (client.current.text(text)) return true;
        const disconnected = client.current;
        client.current = null;
        connectionGeneration.current++;
        void disconnected.close();
        setLive(false);
      }
      // With no connected voice session, typing uses HTTP directly. Never retry a
      // socket turn here: its outcome may be unknown and replay could duplicate intent.
      setNotice(null);
      void rawCommerceCall<{ reply: string; structured: unknown }>(
        'agent/turn',
        {
          method: 'POST',
          body: { message: text },
          idempotencyKey: crypto.randomUUID(),
        },
      )
        .then((result) => {
          if (generation !== textGeneration.current) return;
          const projected = projectTurn(result.structured);
          setReply(result.reply);
          setItems(projected.items);
          setProposal(projected.proposal);
          setPhase('idle');
        })
        .catch((error) => {
          if (generation === textGeneration.current) {
            setNotice(error.message);
            setPhase('idle');
          }
        });
      return true;
    },
    [clearTimers],
  );

  const cartUpdated = useCallback((cartId: string, eventId: string) => {
    preserveCartShelf.current = true;
    if (client.current?.cartUpdated(cartId, eventId)) return;
    const generation = textGeneration.current;
    void rawCommerceCall<{ reply: string; structured: unknown }>('agent/turn', {
      method: 'POST', idempotencyKey: eventId,
      body: { message: 'Cart updated', cart_id: cartId, cart_event_id: eventId },
    }).then(result => {
      // A later buyer request wins; cart acknowledgements are not new buyer intent.
      if (generation !== textGeneration.current || !result.reply) return;
      setReply(result.reply);
      // Cart follow-up suggestions must not replace the shelf the buyer selected.
      setProposal(null);
      setPhase('idle');
    }).catch(() => {
      // The cart write already succeeded. Never offer to retry that mutation here.
      if (generation === textGeneration.current)
        setNotice('Your cart was updated, but the follow-up suggestion is unavailable.');
    });
  }, []);

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
      cartUpdated,
      connect,
      startListening,
      finishListening,
      speak,
      reportCartResult,
      checkoutGuidance: (id: string | null | undefined, stage?: string, version?: number) => {
        checkoutContext.current = [id, stage, version];
        return client.current?.checkoutGuidance(id, stage, version);
      },
      reset,
      interrupt,
    }),
    [
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
      cartUpdated,
      connect,
      startListening,
      finishListening,
      speak,
      reportCartResult,
      reset,
      interrupt,
    ],
  );

  return (
    <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>
  );
}

export function useVoiceSession() {
  const context = useContext(VoiceContext);
  if (!context) throw Error('Voice session requires VoiceSessionProvider');
  return context;
}
