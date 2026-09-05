/**
 * The session, driven by a fake socket and a fake sound card.
 *
 * The assertions here are about ORDER and about frames that are easy to forget, because
 * those are the two ways this design fails silently: a barge-in that reaches the server
 * before the speakers stop sounds like an assistant that talks over you, and a missing
 * `playback_ended` leaves the echo gate holding your microphone shut. Neither throws, and
 * neither shows up in a screenshot.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  VoiceSession,
  defaultTicketUrl,
  defaultVoiceUrl,
  voiceGatewayOrigin,
} from "../session";
import type { ServerFrame } from "../wire";

import { fakeAudio, fakeSocketFactory, Recorder, sessionReady, settle } from "./fakes";

const URL = "wss://storefront.test/api/voice/stream";

function chunkHeader(generation: number, seq = 0, bytes = 3200): ServerFrame {
  return {
    type: "speech_chunk",
    seq,
    speech_generation: generation,
    text: "Your current total is Rs 395.",
    sample_rate_hz: 24_000,
    byte_length: bytes,
    deterministic: true,
    encoding: "pcm16le",
  };
}

async function startedSession() {
  const recorder = new Recorder();
  const { connect, sockets } = fakeSocketFactory(recorder);
  const audio = fakeAudio(recorder);
  const session = new VoiceSession({
    url: URL,
    connect,
    openAudio: async () => audio.io,
  });
  session.start();
  sockets[0].open();
  sockets[0].deliver(sessionReady());
  await settle();
  return { session, socket: sockets[0], sockets, audio, recorder };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("microphone frames", () => {
  it("starts the microphone only once the server has stated the format", async () => {
    const recorder = new Recorder();
    const { connect, sockets } = fakeSocketFactory(recorder);
    const audio = fakeAudio(recorder);
    const session = new VoiceSession({ url: URL, connect, openAudio: async () => audio.io });

    session.start();
    sockets[0].open();
    await settle();
    expect(audio.micStarted).toBe(false);

    sockets[0].deliver(sessionReady());
    await settle();
    expect(audio.micStarted).toBe(true);
    expect(session.getState().mic).toBe("live");
  });

  it("substitutes silence rather than withholding a frame when not transmitting", async () => {
    const { audio, recorder } = await startedSession();
    recorder.clear();

    audio.frame({ rms: 0.4 });

    // A frame still went out, of exactly the same length, carrying nothing.
    expect(recorder.shape).toEqual(["send:silence"]);
    expect(recorder.events[0]).toMatchObject({ kind: "binary", bytes: 3200, silent: true });
  });

  it("sends the buyer's own audio while the talk control is held", async () => {
    const { session, audio, recorder } = await startedSession();
    session.setTransmitting(true);
    recorder.clear();

    audio.frame({ rms: 0.4 });

    expect(recorder.shape).toEqual(["send:audio"]);
  });
});

describe("microphone level", () => {
  /** Level after `count` frames of `rms`, smoothed from silence exactly as the session does. */
  function smoothed(rms: number, count: number): number {
    let level = 0;
    for (let index = 0; index < count; index += 1) level += (rms - level) * 0.4;
    return level;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("publishes one reading per display frame, carrying the level after the LAST frame", async () => {
    const paints: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      paints.push(callback);
      return paints.length;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
    const { session, audio } = await startedSession();
    session.setTransmitting(true);
    const levels: number[] = [];
    session.subscribe(() => levels.push(session.getState().micLevel));

    // Three frames before the next paint: three smoothing steps, no render.
    audio.frame({ rms: 0.4 });
    audio.frame({ rms: 0.4 });
    audio.frame({ rms: 0.4 });
    expect(levels).toEqual([]);
    expect(paints).toHaveLength(1);

    paints.splice(0).forEach((paint) => paint(16));
    expect(levels).toEqual([smoothed(0.4, 3)]);

    // A still meter schedules nothing: the level is already what the UI shows.
    audio.frame({ rms: smoothed(0.4, 3) });
    expect(paints).toHaveLength(0);
  });

  it("falls back to a short timer where there is no animation frame", async () => {
    const { session, audio } = await startedSession();
    session.setTransmitting(true);
    // Only the timers the fallback uses: the default fake set installs a fake
    // `requestAnimationFrame` too, which would put one back after it was taken away.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.stubGlobal("requestAnimationFrame", undefined);
    const levels: number[] = [];
    session.subscribe(() => levels.push(session.getState().micLevel));

    audio.frame({ rms: 0.4 });
    audio.frame({ rms: 0.4 });
    vi.advanceTimersByTime(39);
    expect(levels).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(levels).toEqual([smoothed(0.4, 2)]);
  });

  it("zeroes the meter on release and drops the reading that was still due", async () => {
    const paints: FrameRequestCallback[] = [];
    const cancelled: number[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      paints.push(callback);
      return paints.length;
    });
    vi.stubGlobal("cancelAnimationFrame", (handle: number) => {
      cancelled.push(handle);
    });
    const { session, audio } = await startedSession();
    session.setTransmitting(true);
    audio.frame({ rms: 0.4 });
    expect(paints).toHaveLength(1);

    session.setTransmitting(false);
    expect(session.getState().micLevel).toBe(0);
    expect(cancelled).toEqual([1]);

    // The browser would not run a cancelled callback; a stub that does anyway must find
    // it inert, because the level it would publish was zeroed with the release.
    paints.splice(0).forEach((paint) => paint(16));
    expect(session.getState().micLevel).toBe(0);
  });
});

describe("playback scheduling", () => {
  it("butts each chunk against the play head, one lead ahead of the clock", async () => {
    const { socket, recorder } = await startedSession();
    recorder.clear();

    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    socket.deliver(chunkHeader(1, 1));
    socket.deliverBinary(3200);

    const scheduled = recorder.events.filter(
      (event) => event.at === "audio" && event.kind === "schedule",
    );
    expect(scheduled).toHaveLength(2);
    if (scheduled[0].at !== "audio" || scheduled[1].at !== "audio") return;
    if (scheduled[0].kind !== "schedule" || scheduled[1].kind !== "schedule") return;

    // 3200 bytes of PCM16 at 24 kHz is 3200 / 2 / 24000 seconds.
    const duration = 3200 / 2 / 24_000;
    expect(scheduled[0].startAt).toBeCloseTo(0.03, 6); // now + playback_lead_s
    expect(scheduled[1].startAt).toBeCloseTo(0.03 + duration, 6);
    expect(scheduled[0].sampleRateHz).toBe(24_000);
  });

  it("discards a chunk whose bytes do not match its announced length", async () => {
    const { session, socket, audio } = await startedSession();
    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0, 3200));
    socket.deliverBinary(2048);

    expect(audio.scheduledChunks()).toBe(0);
    expect(session.getState().notice?.kind).toBe("speech_chunk_mismatch");
  });
});

describe("playback_ended", () => {
  it("is sent when the last chunk of a generation finishes playing", async () => {
    const { socket, audio, recorder } = await startedSession();

    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    socket.deliver({ type: "speech_end", speech_generation: 1, chunks: 1, cancelled: false });

    // The server has finished sending; the speakers have not finished playing.
    expect(recorder.sentFrames()).not.toContainEqual({
      type: "playback_ended",
      speech_generation: 1,
    });

    audio.finishOldestChunk();

    expect(recorder.sentFrames()).toContainEqual({
      type: "playback_ended",
      speech_generation: 1,
    });
  });

  it("waits for the last chunk when the audio drains before speech_end arrives", async () => {
    const { socket, audio, recorder } = await startedSession();

    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    audio.finishOldestChunk();

    expect(recorder.sentFrames()).not.toContainEqual({
      type: "playback_ended",
      speech_generation: 1,
    });

    socket.deliver({ type: "speech_end", speech_generation: 1, chunks: 1, cancelled: false });

    expect(recorder.sentFrames()).toContainEqual({
      type: "playback_ended",
      speech_generation: 1,
    });
  });

  it("is sent exactly once per generation", async () => {
    const { socket, audio, recorder } = await startedSession();

    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    socket.deliver(chunkHeader(1, 1));
    socket.deliverBinary(3200);
    socket.deliver({ type: "speech_end", speech_generation: 1, chunks: 2, cancelled: false });
    audio.finishOldestChunk();
    audio.finishOldestChunk();

    const reports = recorder
      .sentFrames()
      .filter((frame) => frame.type === "playback_ended");
    expect(reports).toHaveLength(1);
  });
});

describe("barge-in", () => {
  it("flushes local playback BEFORE the barge_in frame is sent", async () => {
    const { session, socket, audio, recorder } = await startedSession();

    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);

    session.setTransmitting(true);
    recorder.clear();

    // Sustain is 0.3 s and each frame is 0.1 s, so the fourth frame crosses it.
    for (let index = 0; index < 4; index += 1) audio.frame({ rms: 0.5 });

    const stopAt = recorder.shape.indexOf("audio:stop");
    const bargeAt = recorder.shape.indexOf("send:barge_in");
    expect(stopAt).toBeGreaterThanOrEqual(0);
    expect(bargeAt).toBeGreaterThanOrEqual(0);
    // The ordering IS the property: local action first, server reconciliation second.
    expect(stopAt).toBeLessThan(bargeAt);
    expect(recorder.shape.filter((event) => !event.startsWith("send:audio"))).toEqual([
      "audio:stop",
      "send:barge_in",
    ]);
  });

  it("does not fire on a level that is not sustained", async () => {
    const { session, socket, audio, recorder } = await startedSession();
    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    session.setTransmitting(true);
    recorder.clear();

    // A cough: loud, then quiet again, then loud. Never 0.3 s in a row.
    audio.frame({ rms: 0.5 });
    audio.frame({ rms: 0.5 });
    audio.frame({ rms: 0.001 });
    audio.frame({ rms: 0.5 });
    audio.frame({ rms: 0.5 });

    expect(recorder.shape).not.toContain("send:barge_in");
  });

  it("does not report playback_ended for the speech it just cut off", async () => {
    const { session, socket, audio, recorder } = await startedSession();
    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    session.setTransmitting(true);
    for (let index = 0; index < 4; index += 1) audio.frame({ rms: 0.5 });

    // `playback_ended` starts the server's echo tail. Sending it here would gate the
    // microphone across the very sentence the buyer interrupted with.
    socket.deliver({ type: "speech_end", speech_generation: 1, chunks: 1, cancelled: true });
    expect(recorder.sentFrames().filter((frame) => frame.type === "playback_ended")).toHaveLength(
      0,
    );
  });

  it("discards audio that arrives for a generation already interrupted", async () => {
    const { session, socket, audio } = await startedSession();
    socket.deliver({ type: "speech_start", speech_generation: 1 });
    socket.deliver(chunkHeader(1, 0));
    socket.deliverBinary(3200);
    session.setTransmitting(true);
    for (let index = 0; index < 4; index += 1) audio.frame({ rms: 0.5 });
    expect(audio.scheduledChunks()).toBe(1);

    // Already on the wire when the buyer interrupted.
    socket.deliver(chunkHeader(1, 1));
    socket.deliverBinary(3200);
    expect(audio.scheduledChunks()).toBe(1);

    // The next generation plays normally.
    socket.deliver({ type: "speech_start", speech_generation: 2 });
    socket.deliver(chunkHeader(2, 0));
    socket.deliverBinary(3200);
    expect(audio.scheduledChunks()).toBe(2);
  });
});

describe("connection", () => {
  it("reconnects with exponential backoff up to a ten second ceiling", () => {
    vi.useFakeTimers();
    const recorder = new Recorder();
    const { connect, sockets } = fakeSocketFactory(recorder);
    const audio = fakeAudio(recorder);
    const session = new VoiceSession({ url: URL, connect, openAudio: async () => audio.io });

    session.start();
    sockets[0].open();
    sockets[0].serverClose();
    expect(session.getState().connection).toBe("reconnecting");

    // 0.5 s, then 1, 2, 4, 8, and then the ceiling rather than 16.
    const expected = [500, 1_000, 2_000, 4_000, 8_000, 10_000];
    expected.forEach((delay, index) => {
      vi.advanceTimersByTime(delay - 1);
      expect(sockets).toHaveLength(index + 1);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(index + 2);
      sockets[index + 1].serverClose();
    });

    expect(session.getState().notice?.kind).toBe("connection_lost");
  });

  it("does not reconnect after the buyer stops the session", () => {
    vi.useFakeTimers();
    const recorder = new Recorder();
    const { connect, sockets } = fakeSocketFactory(recorder);
    const audio = fakeAudio(recorder);
    const session = new VoiceSession({ url: URL, connect, openAudio: async () => audio.io });

    session.start();
    sockets[0].open();
    session.stop();
    sockets[0].serverClose();

    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
    expect(session.getState().connection).toBe("closed");
  });

  it("surfaces a frame it could not read instead of ignoring it", async () => {
    const { session, socket } = await startedSession();
    socket.deliverRaw("{\"type\":\"transcript_partial\"}");
    expect(session.getState().notice?.kind).toBe("frame_unreadable");
  });
});

describe("text input", () => {
  it("sends typed text, which stays available whatever speech is doing", async () => {
    const { session, recorder } = await startedSession();
    expect(session.sendText("  two kilos of onions  ")).toBe(true);
    expect(recorder.sentFrames()).toContainEqual({
      type: "text_input",
      text: "two kilos of onions",
    });
  });

  it("refuses what the contract would refuse", async () => {
    const { session } = await startedSession();
    expect(session.sendText("   ")).toBe(false);
    expect(session.sendText("x".repeat(4001))).toBe(false);
  });
});

describe("gateway addressing", () => {
  const original = process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
  afterEach(() => {
    if (original === undefined) delete process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
    else process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN = original;
  });

  it("addresses the gateway socket directly in development, which is what the CSP permits", () => {
    // csp.ts names ws://127.0.0.1:8100 in connect-src only when NODE_ENV !== production.
    expect(voiceGatewayOrigin()).toBe("http://127.0.0.1:8100");
    expect(defaultVoiceUrl()).toBe("ws://127.0.0.1:8100/v1/voice/stream");
  });

  it("mints its ticket same-origin even in development, because only this server has the bearer", () => {
    // The socket goes straight to :8100 above; the ticket cannot. The gateway wants an
    // Authorization header the page has never held -- the bearer is in an httpOnly cookie
    // -- so a direct mint would arrive anonymous and be refused. `/api/voice/tickets` is
    // the route that has the cookie.
    expect(defaultTicketUrl()).toBe(`${window.location.origin}/api/voice/tickets`);
  });

  it("keeps minting same-origin when a deployment moves the gateway elsewhere", () => {
    process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN = "https://voice.example.test/";
    expect(defaultVoiceUrl()).toBe("wss://voice.example.test/v1/voice/stream");
    expect(defaultTicketUrl()).toBe(`${window.location.origin}/api/voice/tickets`);
  });

  it("honours an explicit origin, for a deployment that widened its own policy", () => {
    process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN = "https://voice.example.test/";
    expect(voiceGatewayOrigin()).toBe("https://voice.example.test");
    expect(defaultVoiceUrl()).toBe("wss://voice.example.test/v1/voice/stream");
  });
});
