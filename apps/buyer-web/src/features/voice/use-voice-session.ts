/**
 * The connection hook: one `VoiceSession` per mounted panel, exposed to React.
 *
 * All the difficult behaviour is in `session.ts` -- the socket, the microphone, the
 * playback queue, the barge-in ordering -- and this file is the seam that puts it in front
 * of a component. It is deliberately thin: a hook that also owned the audio graph would be
 * a hook that could only be tested by rendering it, and the rules it enforces are worth
 * more than that.
 *
 * `useSyncExternalStore` rather than `useState` because the session is driven by a socket
 * and an audio callback, both of which fire outside React's knowledge and at a rate
 * (ten microphone frames a second) that would otherwise queue ten renders a second whether
 * or not anything changed. The session hands back the same state object when a frame
 * changed nothing, so the store's identity check does the throttling for free.
 */
"use client";

import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";

import {
  defaultVoiceUrl,
  VoiceSession,
  type AudioIOFactory,
  type SocketFactory,
  type VoiceSessionState,
} from "./session";

export interface UseVoiceSessionOptions {
  /** Defaults to same-origin `wss://.../api/voice/stream`; see `defaultVoiceUrl`. */
  url?: string;
  /** Injected in tests. Production uses the browser `WebSocket`. */
  connect?: SocketFactory;
  /** Injected in tests. Production opens an `AudioContext` and the microphone. */
  openAudio?: AudioIOFactory;
}

export interface VoiceSessionController extends VoiceSessionState {
  /** Opens the socket and the audio device. Call from a user gesture. */
  start: () => void;
  stop: () => void;
  setTransmitting: (on: boolean) => void;
  sendText: (text: string) => boolean;
  /** Ask for a named approval card to be read aloud; see `VoiceSession.readCard`. */
  readCard: (checkoutId: string, version: number, locale?: "en-IN" | "hi-IN") => boolean;
  dismissDegradation: (id: string) => void;
  dismissNotice: () => void;
}

export function useVoiceSession(options: UseVoiceSessionOptions = {}): VoiceSessionController {
  const { url, connect, openAudio } = options;
  const resolvedUrl = url ?? defaultVoiceUrl();

  const session = useMemo(
    () => new VoiceSession({ url: resolvedUrl, connect, openAudio }),
    [resolvedUrl, connect, openAudio],
  );

  // A session left running after the panel closes holds an open microphone. Whatever else
  // is ambiguous about teardown, that is not.
  useEffect(() => () => session.stop(), [session]);

  const state = useSyncExternalStore(session.subscribe, session.getState, session.getState);

  const start = useCallback(() => session.start(), [session]);
  const stop = useCallback(() => session.stop(), [session]);
  const setTransmitting = useCallback((on: boolean) => session.setTransmitting(on), [session]);
  const sendText = useCallback((text: string) => session.sendText(text), [session]);
  const readCard = useCallback(
    (checkoutId: string, version: number, locale?: "en-IN" | "hi-IN") =>
      session.readCard(checkoutId, version, locale),
    [session],
  );
  const dismiss = useCallback((id: string) => session.dismissDegradation(id), [session]);
  const dismissNotice = useCallback(() => session.dismissNotice(), [session]);

  return {
    ...state,
    start,
    stop,
    setTransmitting,
    sendText,
    readCard,
    dismissDegradation: dismiss,
    dismissNotice,
  };
}
