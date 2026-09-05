/**
 * The speech playback queue: schedule chunks ahead of the clock, and say when they stop.
 *
 * The server announces each chunk with a `speech_chunk` frame and then sends exactly one
 * binary frame of that many bytes, PCM16 little-endian mono at 24 kHz. Each chunk is
 * scheduled at `max(now + playback_lead_s, playHead)` and the play head advances by the
 * chunk's own duration, so consecutive chunks butt up against each other with no gap and
 * no overlap. Scheduling everything at `now` instead produces a stutter on every chunk
 * boundary; scheduling at `playHead` alone underruns the first time a chunk is late.
 *
 * Two things here are load-bearing and neither is obvious.
 *
 *  1. **`playback_ended` is reported from here, not from the socket handler.** The echo
 *     tail (19.6) measures from the moment the buyer's SPEAKERS fall quiet, which is this
 *     queue draining, not the server finishing its send. A deeply buffered client that
 *     never reports playback end leaves the server's echo gate open for its whole maximum
 *     hold, replacing the buyer's microphone frames with silence the entire time. From
 *     the buyer's side that is indistinguishable from the assistant having stopped
 *     listening -- and it degrades visibly, late, and for no reason a user could guess.
 *
 *  2. **A generation counter guards every chunk** (19.8). A barge-in cancels work already
 *     in flight, and audio for a cancelled generation can still be on the wire after the
 *     interruption. Chunks below `minAccepted` are dropped rather than played, because a
 *     sentence the buyer already interrupted must not reach their ears.
 *
 * The queue is written against a small `AudioOutput` port rather than `AudioContext`
 * directly, so the ordering rules above can be tested without a sound card.
 */
import type { SpeechChunkHeader } from "./wire";

/** A chunk handed to the device, still cancellable. */
export interface ScheduledSource {
  /** Stop immediately and drop the scheduled play. Must not fire the ended callback. */
  stop(): void;
}

/** Everything the queue needs from the audio device. */
export interface AudioOutput {
  /** Seconds on the device clock. Monotonic, and the same clock `schedule` takes. */
  now(): number;
  /** Play PCM16 LE mono at `at` seconds on that clock. `onEnded` fires when it finishes. */
  schedule(
    pcm: ArrayBuffer,
    sampleRateHz: number,
    at: number,
    onEnded: () => void,
  ): ScheduledSource;
  close(): void;
}

export interface PlaybackQueueOptions {
  output: AudioOutput;
  /** `playback_lead_s` from `session_ready`. Never a constant of this file. */
  leadS: number;
  /** Called exactly once per generation, when this client's speakers actually stop. */
  onGenerationEnded: (speechGeneration: number) => void;
  /** A chunk that did not match its announced length, for the caller to surface. */
  onMalformedChunk?: (header: SpeechChunkHeader, actualBytes: number) => void;
}

const BYTES_PER_SAMPLE = 2;

export class PlaybackQueue {
  private readonly output: AudioOutput;
  private readonly leadS: number;
  private readonly onGenerationEnded: (speechGeneration: number) => void;
  private readonly onMalformedChunk: (header: SpeechChunkHeader, actualBytes: number) => void;

  private readonly sources = new Set<ScheduledSource>();
  private generation = 0;
  /** Chunks below this generation are refused: they belong to a cancelled utterance. */
  private minAccepted = 0;
  private playHead = 0;
  private outstanding = 0;
  private serverEnded = false;
  private endedReported = false;
  private closed = false;

  constructor(options: PlaybackQueueOptions) {
    this.output = options.output;
    this.leadS = options.leadS;
    this.onGenerationEnded = options.onGenerationEnded;
    this.onMalformedChunk = options.onMalformedChunk ?? (() => {});
  }

  /** Seconds of audio still scheduled ahead of the clock. Zero when the speakers are idle. */
  get queuedSeconds(): number {
    return Math.max(0, this.playHead - this.output.now());
  }

  get currentGeneration(): number {
    return this.generation;
  }

  /**
   * A new stream: forget every generation this queue has seen.
   *
   * Called on `session_ready`, including the one that follows a reconnect. Generations are
   * numbered per session, so a queue that kept `minAccepted` from the previous socket would
   * refuse every chunk of the new one and go permanently, silently mute.
   */
  reset(): void {
    if (this.closed) return;
    this.stopAll();
    this.generation = 0;
    this.minAccepted = 0;
    this.playHead = this.output.now();
    this.outstanding = 0;
    this.serverEnded = false;
    this.endedReported = false;
  }

  /** `speech_start`: a new utterance begins. */
  begin(speechGeneration: number): void {
    if (this.closed || speechGeneration < this.minAccepted) return;
    this.stopAll();
    this.generation = speechGeneration;
    this.minAccepted = speechGeneration;
    this.playHead = this.output.now();
    this.outstanding = 0;
    this.serverEnded = false;
    this.endedReported = false;
  }

  /**
   * One announced chunk and its audio. Returns false when the chunk was refused.
   *
   * The length check is not paranoia about the server: it is the pairing rule of the
   * contract. The binary frame that follows a header is that header's audio, and a
   * mismatch means the stream has desynchronised. Playing it anyway emits noise at the
   * wrong rate, which sounds like the assistant breaking down.
   */
  enqueue(header: SpeechChunkHeader, audio: ArrayBuffer): boolean {
    if (this.closed) return false;
    if (header.speech_generation < this.minAccepted) return false;
    if (header.speech_generation > this.generation) this.begin(header.speech_generation);

    if (audio.byteLength !== header.byte_length || audio.byteLength % BYTES_PER_SAMPLE !== 0) {
      this.onMalformedChunk(header, audio.byteLength);
      return false;
    }
    if (audio.byteLength === 0) return false;

    const duration = audio.byteLength / BYTES_PER_SAMPLE / header.sample_rate_hz;
    const startAt = Math.max(this.output.now() + this.leadS, this.playHead);
    const generation = header.speech_generation;

    const source = this.output.schedule(audio, header.sample_rate_hz, startAt, () => {
      this.sources.delete(source);
      if (generation !== this.generation) return;
      this.outstanding = Math.max(0, this.outstanding - 1);
      this.reportIfDrained();
    });

    this.sources.add(source);
    this.playHead = startAt + duration;
    this.outstanding += 1;
    return true;
  }

  /** `speech_end`: the server has sent everything it is going to send for `generation`. */
  serverFinished(speechGeneration: number): void {
    if (this.closed || speechGeneration !== this.generation) return;
    this.serverEnded = true;
    this.reportIfDrained();
  }

  /**
   * Barge-in, or teardown: stop this client's audio now.
   *
   * Called BEFORE the `barge_in` frame goes out (19.7). The local action is what the buyer
   * hears; the server round trip is reconciliation, and waiting for it means the assistant
   * talks over them for a full RTT. This is only safe because stopping audio is
   * reversible -- the same pattern is never applied to money.
   *
   * A flush deliberately does NOT report `playback_ended`. That frame starts the server's
   * echo tail, during which microphone frames are replaced with silence; a barge-in is
   * precisely the moment the buyer IS speaking, so starting an echo tail here would eat
   * the first half second of the interruption that caused it. The server learns the
   * speakers went quiet from `barge_in` itself, which cannot be mistaken for a normal end.
   */
  flush(): void {
    this.stopAll();
    this.outstanding = 0;
    this.playHead = this.output.now();
    // Nothing from this utterance may be played again, including audio already in flight.
    this.minAccepted = this.generation + 1;
    // A late `speech_end` for the cancelled generation must not fire the report either.
    this.endedReported = true;
  }

  /** `interrupted`: the server names the generation that is now current. */
  adopt(speechGeneration: number): void {
    if (this.closed) return;
    if (speechGeneration === this.generation && this.minAccepted > this.generation) return;
    this.stopAll();
    this.generation = speechGeneration;
    this.minAccepted = speechGeneration;
    this.playHead = this.output.now();
    this.outstanding = 0;
    this.serverEnded = false;
    this.endedReported = false;
  }

  close(): void {
    if (this.closed) return;
    this.stopAll();
    this.closed = true;
    this.output.close();
  }

  private reportIfDrained(): void {
    if (this.serverEnded && this.outstanding === 0) this.reportEnded();
  }

  private reportEnded(): void {
    if (this.endedReported) return;
    this.endedReported = true;
    this.onGenerationEnded(this.generation);
  }

  private stopAll(): void {
    for (const source of this.sources) source.stop();
    this.sources.clear();
  }
}

/**
 * The Web Audio implementation of the port.
 *
 * PCM16 is read through a `DataView` with an explicit little-endian flag rather than cast
 * to an `Int16Array`. Every browser this ships to is little-endian and the cast would work
 * today; naming the byte order costs one line and means the code says what the contract
 * says instead of relying on the platform to agree with it.
 */
export function webAudioOutput(context: AudioContext): AudioOutput {
  return {
    now: () => context.currentTime,
    schedule(pcm, sampleRateHz, at, onEnded) {
      const view = new DataView(pcm);
      const sampleCount = Math.floor(pcm.byteLength / BYTES_PER_SAMPLE);
      const buffer = context.createBuffer(1, sampleCount, sampleRateHz);
      const channel = buffer.getChannelData(0);
      for (let index = 0; index < sampleCount; index += 1) {
        channel[index] = view.getInt16(index * BYTES_PER_SAMPLE, true) / 32768;
      }
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      source.onended = onEnded;
      source.start(at);
      return {
        stop() {
          // A cancelled chunk must not report that it finished playing: it did not.
          source.onended = null;
          try {
            source.stop();
          } catch {
            // Already finished. Stopping a source twice is not an error worth surfacing.
          }
          source.disconnect();
        },
      };
    },
    close() {
      // The context is the caller's: capture and playback share one clock, and closing it
      // from under the microphone would take the other half of the conversation with it.
    },
  };
}
