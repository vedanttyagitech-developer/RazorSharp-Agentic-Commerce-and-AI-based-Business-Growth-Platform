/**
 * The public face of the voice feature.
 *
 * A caller mounts `VoicePanel` and passes nothing; everything below it -- the socket, the
 * microphone, the playback queue, the barge-in rules -- is this feature's own business.
 * The smaller pieces are exported because a surface that already has a transcript of its
 * own may want only the control or only the notices, and because a test should be able to
 * reach the pure parts without rendering anything.
 *
 * Nothing exported here can approve, pay, refund or cancel. That is not enforced by this
 * barrel -- it is enforced by there being no such capability anywhere under it.
 */
export { VoicePanel, type VoicePanelProps } from "./voice-panel";
export { VoiceConsent, type VoiceConsentProps } from "./voice-consent";
export { PushToTalk, type PushToTalkProps } from "./push-to-talk";
export { LiveTranscript, type LiveTranscriptProps } from "./live-transcript";
export { ClientNoticeCard, DegradedNotice } from "./degraded-notice";

export {
  useVoiceSession,
  type UseVoiceSessionOptions,
  type VoiceSessionController,
} from "./use-voice-session";

export {
  defaultTicketUrl,
  defaultVoiceUrl,
  mintVoiceTicket,
  ticketedSocket,
  voiceGatewayOrigin,
  VoiceSession,
  type AudioIO,
  type AudioIOFactory,
  type ClientNotice,
  type ClientNoticeKind,
  type ConnectionState,
  type MicState,
  type SocketFactory,
  type VoiceSessionOptions,
  type VoiceSessionState,
  type TicketMinter,
  type VoiceSocket,
  type VoiceSocketHandlers,
  type VoiceTicket,
} from "./session";

export {
  idleConsent,
  initialTranscriptState,
  reduceTranscript,
  type ConsentState,
  type ConsentStatus,
  type DegradationNotice,
  type HeldTurn,
  type TranscriptEntry,
  type VoiceTranscriptState,
} from "./transcript";

export {
  applyStreamText,
  CONSENT_CLOSED_REASONS,
  DEGRADATION_KINDS,
  parseServerFrame,
  type AgentReply,
  type CardRead,
  type ClientFrame,
  type ConsentClosed,
  type ConsentClosedReason,
  type ConsentDeclined,
  type ConsentListening,
  type ConsentRecognised,
  type ConsentUnrecognised,
  type Degradation,
  type DegradationKind,
  type ReadCard,
  type ServerFrame,
  type SessionReady,
  type SpeechChunkHeader,
  type TranscriptFinal,
  type TranscriptPartial,
} from "./wire";
