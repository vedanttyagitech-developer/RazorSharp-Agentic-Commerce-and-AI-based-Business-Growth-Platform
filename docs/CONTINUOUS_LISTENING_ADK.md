# Continuous Listening with Gemini Transcribe, inside Google ADK

### A field guide to always-on speech input that still feels like a conversation

---

## What this document is

Everything below is drawn from building and debugging a live voice assistant in this
repository, plus a line-by-line read of the **installed** `google-adk 2.7.1` source at
`.venv/lib/python3.12/site-packages/google/adk/`.

Two rules were followed while writing it:

- **Every API signature was copied out of the installed package**, not recalled from
  documentation. Version drift between ADK releases is real, and a signature that is
  subtly wrong is worse than no signature at all, because you will paste it.
- **Every bug described here actually happened.** The failure modes are not
  hypotheticals; each one cost days.

**Honest status of ADK in this repo.** AgentFlow does *not* run on ADK today. The only
`google.adk` usage anywhere is a presence probe in `routes/adk.py:288` that reports
whether the package imports. The orchestrator uses its own LangGraph loop. So Part 3
onward is a **design grounded in ADK's real source**, not a description of running code —
it is labelled where that distinction matters.

---

## Part 1 — Why "continuous listening" is hard

The naive mental model is a loop: record until silence, transcribe, respond, repeat. That
model produces a walkie-talkie, and every attempt to make it feel conversational breaks it
in a different place. Three failures, in the order we hit them.

### 1.1 The recognizer dies while the assistant talks

The first implementation used `google-cloud-speech` streaming recognition with
`single_utterance=True`. Google closed the stream after every utterance, and — worse —
after any stretch of initial silence, with:

```
400 Audio Timeout Error: Long duration elapsed without audio
```

While the assistant spoke, no user audio arrived. The stream timed out. The user's
*second* sentence landed on a dead socket and vanished. From the outside this reads as
"she doesn't listen to me any more", which is what the bug report said.

The worker spent its life restarting. And the restart logic made it worse: it **drained
the audio queue** on each reconnect, discarding whatever arrived during the gap. Speech
that began at the wrong moment simply disappeared.

> **Rule 1.** The recognition session must outlive the utterance. If your recognizer's
> lifecycle is per-turn, you will spend the rest of the project fighting the gaps between
> turns.

### 1.2 She transcribes herself

Once the stream was persistent, a second failure appeared: the assistant would speak one
sentence and stop dead.

Cause: **browser echo cancellation does not reliably cancel Web Audio playback.** The
`echoCancellation` constraint on `getUserMedia` handles audio the browser itself is
playing through a media element; audio you synthesize through an `AudioContext` is not
reliably part of what the AEC knows about. Her own TTS came back through the microphone,
was transcribed as user speech, and the turn-taking logic read it as an interruption.

She interrupted herself, then transcribed the interruption, then interrupted that.

### 1.3 The transcript ate itself

The third failure produced text like:

```
Tumjo mainejo maine bookTu MeriTu MainuTuMainuTu MainuTu Meri
```

Cause: the client appended each incoming transcript frame to what it held. But streaming
recognizers send the **whole current hypothesis** every frame and freely **revise** it
mid-utterance. A revision does not extend the previous string — so every revision was
concatenated onto the last.

This one was a specification failure, not a coding error. The wire contract defined the
*shape* of a text frame and never said whether `text` was cumulative or a delta. The
backend chose cumulative; the frontend guessed and wrote a "handles both" heuristic.

> **Rule 2.** For every streamed field, write down in the contract: cumulative or delta,
> revisable or monotonic, and what an empty value means. Shape is not semantics.

---

## Part 2 — Architecture A: your own transcriber

This is what runs in this repo today: `services/gateway/agentflow_gateway/live/transcribe.py`.

### 2.1 The shape

One persistent `google-genai` Live session, used purely as a recognizer:

```
model    gemini-3.5-transcribe-live-preview     (Vertex, location "global")
client   genai.Client(vertexai=True, project=..., location="global")
connect  client.aio.live.connect(model=..., config=cfg)
cfg      {"response_modalities": ["TEXT"], "input_audio_transcription": {}}
send     await s.send_realtime_input(
             audio=types.Blob(data=<pcm16>, mime_type="audio/pcm;rate=16000"))
recv     msg.voice_activity.voice_activity_type    -> ACTIVITY_START / ACTIVITY_END
         msg.server_content.interim_input_transcription.text  -> cumulative
         msg.server_content.input_transcription.text          -> FINAL
```

Three constraints that are not obvious and will cost you an afternoon each:

- **`input_audio_transcription` is mandatory.** Omit it and the server closes the socket
  with `1007 Input audio transcription is required for ASR`. Not a validation error — a
  close. Record this in a comment so nobody "optimises" the empty-looking field away.
- **The 3.5 models serve from `"global"` only.** A regional endpoint 404s. This is
  hard-coded (`TRANSCRIBE_LOCATION = "global"`) and deliberately ignores
  `GOOGLE_CLOUD_LOCATION`.
- **Sample rate travels in the MIME string**, not in a config field. `PCM16-LE mono` is
  assumed and never asserted.

### 2.2 The lifecycle that makes it continuous

```python
class GeminiTranscriber:
    def __init__(self, *, model: str | None = None) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = asyncio.Event()

        self.on_interim: TextCallback | None = None        # cumulative
        self.on_final: TextCallback | None = None          # the turn
        self.on_activity_start: VoidCallback | None = None # server VAD
        self.on_activity_end: VoidCallback | None = None
```

`connect()` starts **one background task** and waits at most 20 s on an event. A timeout
logs a warning and **returns normally**, leaving the loop retrying underneath.

> That is deliberate and worth copying: `connect()` returning is *not* proof the stream is
> up. The alternative — raising — takes the whole voice session down for a transient
> network blip, which is exactly when you least want to drop the user.

Per connection the task spawns **two child tasks**: `_send_loop` drains the queue into
`send_realtime_input`, `_receive_loop` iterates `session.receive()`. Whichever finishes
first tears down the pair and triggers a reconnect. Backoff is exponential from `0.5 s` to
a `10 s` ceiling, reset on every successful connect.

### 2.3 The queue: bound it by dropping the oldest, never by draining

```python
_MAX_QUEUED_FRAMES = 1500      # ~30 s of 20 ms mic frames
```

```python
async def feed(self, pcm: bytes) -> None:
    while self._queue.qsize() >= _MAX_QUEUED_FRAMES:
        try:
            self._queue.get_nowait()      # evict the OLDEST
        except asyncio.QueueEmpty:
            break
    self._queue.put_nowait(pcm)
```

`feed()` never blocks and never raises. Three properties matter:

1. **The queue survives reconnects.** Audio that arrives mid-reconnect is still there
   afterwards. The original bug was clearing it.
2. **Eviction is from the front.** Dropping the newest frame would discard the speech the
   user is producing right now; dropping the oldest discards audio already stale.
3. **A bound exists at all.** A producer faster than the socket otherwise grows memory
   without limit.

### 2.4 Interim vs final, and the cumulative rule

Which attribute is populated decides everything:

| field | meaning | client behaviour |
|---|---|---|
| `interim_input_transcription.text` | cumulative, **revisable** | replace held text |
| `input_transcription.text` | settled utterance = the turn | replace, then close the turn |

Client side, the entire fix is two lines:

```ts
function applyStreamText(current: string, incoming: string): string {
  return incoming || current;   // replace, never append
}
```

`|| current` simultaneously implements *replace* and the rule "an empty frame must not
clear the turn".

### 2.5 `[defect]` Observability is too thin — fix this before you ship

Two problems in our own implementation, both worth fixing in yours:

- The message handler swallows callback exceptions with `logger.debug`. If **your**
  `on_final` raises, the turn is lost silently. Log that at `warning` or `exception`.
- `generation_complete` is documented in the header as "turn end" and is **never read**.
  There is no turn-end callback. The only end-of-utterance signal available today is
  `on_final`.

Frame counters exist (`_frames_in`, `_frames_sent`) but nothing in the repo ever reads
them. Counters you do not surface are not observability.

---

## Part 3 — Architecture B: inside Google ADK

Now the real ADK surface, read from the installed 2.7.1 source.

### 3.1 The entry point

```python
# runners.py:1738
async def run_live(
    self,
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    live_request_queue: LiveRequestQueue,
    run_config: Optional[RunConfig] = None,
    session: Optional[Session] = None,
) -> AsyncGenerator[Event, None]:
```

Keyword-only. It yields events for the **whole conversation**, not one turn: after every
`turn_complete` the receive loop re-enters `llm_connection.receive()`, so a single
`run_live` call spans an unbounded number of turns until you call
`live_request_queue.close()` or the socket dies.

Two gotchas:

- **It mutates the `RunConfig` you pass in.** If `response_modalities is None` it sets
  `[types.Modality.AUDIO]` on your object (`runners.py:1803`). Do not share one config
  across sessions.
- **`StreamingMode.BIDI` is a red herring.** The enum's own docstring says `run_live`
  "uses a completely different code path that doesn't rely on `streaming_mode`". Setting
  it changes nothing for live.

### 3.2 The input side

```python
# agents/live_request_queue.py:66
class LiveRequestQueue:
    def close(self) -> None: ...
    def send_content(self, content: types.Content, partial: bool = False) -> None: ...
    def send_realtime(self, blob: types.Blob) -> None: ...
    def send_activity_start(self) -> None: ...
    def send_activity_end(self) -> None: ...
    def send_audio_stream_end(self) -> None: ...
    def send(self, req: LiveRequest) -> None: ...
```

Every `send_*` is **synchronous** (`put_nowait`) — do not `await` them. There is no
`send_audio()`, no `send_blob()`, no `send_text()`.

Audio in:

```python
queue.send_realtime(types.Blob(data=pcm16_bytes, mime_type="audio/pcm;rate=16000"))
```

Model audio comes back on `event.content.parts[0].inline_data` with
`mime_type="audio/pcm;rate=24000"`. **16 kHz in, 24 kHz out** — the asymmetry is real and
intentional; do not "fix" it.

Three traps:

- **The queue is unbounded.** No backpressure. A mic producing faster than the socket
  drains grows memory. Bound it yourself, exactly as in §2.3.
- **`close()` does not shut the queue down** — it enqueues a sentinel, so it is ordered
  *behind* everything already queued. Audio still buffered gets sent first.
- **Request priority is `activity_start > activity_end > audio_stream_end > blob >
  content`.** A single `LiveRequest` carrying both a blob and `activity_end` fires only
  the `activity_end`; the audio is dropped.

### 3.3 Configuration

```python
# agents/run_config.py
response_modalities: Optional[list[types.Modality]] = None
output_audio_transcription: Optional[types.AudioTranscriptionConfig] = Field(
    default_factory=types.AudioTranscriptionConfig)
input_audio_transcription: Optional[types.AudioTranscriptionConfig] = Field(
    default_factory=types.AudioTranscriptionConfig)
realtime_input_config: Optional[types.RealtimeInputConfig] = None
session_resumption: Optional[types.SessionResumptionConfig] = None
context_window_compression: Optional[types.ContextWindowCompressionConfig] = None
proactivity: Optional[types.ProactivityConfig] = None
enable_affective_dialog: Optional[bool] = None
```

**Good news:** both transcription configs default to *on* via `default_factory`. The
`1007` trap from §2.1 cannot happen here — ADK sends them unless you explicitly pass
`None`.

### 3.4 Turn-taking: server VAD or manual, never both

ADK makes **no** turn-taking decisions. It is a faithful pipe; Gemini Live decides
server-side. The knobs live on `google.genai.types`, and ADK re-exports them untouched.

```python
run_config = RunConfig(
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False,
            silence_duration_ms=...,      # end-of-turn silence threshold
            prefix_padding_ms=...,        # audio kept before detected speech
            # start_of_speech_sensitivity=..., end_of_speech_sensitivity=...
        ),
        activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        turn_coverage=...,
    ),
)
```

Every field defaults to `None` and the **server** picks. The installed source contains no
defaults, so do not assume a number — measure.

Two regimes, and mixing them is a bug:

| regime | how a turn ends | flush call |
|---|---|---|
| automatic VAD (default) | server detects silence | `send_audio_stream_end()` |
| `disabled=True` | you call `send_activity_start()` / `send_activity_end()` | — |

`LiveRequest.audio_stream_end`'s docstring says it "is only used when Voice Activity
Detection is enabled" — the opposite regime from the activity signals. **ADK performs no
validation** that VAD is actually disabled before forwarding activity signals; send them
in the wrong regime and the server rejects them.

Also note the transport warning: `send_realtime_input` is "optimized for responsiveness at
the expense of deterministic ordering". Ordering between an activity signal and adjacent
audio is **not guaranteed**.

### 3.5 What ADK gives you free

**Reconnection.** `BaseLlmFlow.run_live` stores a resumption handle on the invocation
context, converts a server `go_away` into an internal `_ReconnectSentinel`, and
reconnects silently, up to `DEFAULT_MAX_RECONNECT_ATTEMPTS = 5`. This is the whole of
§2.2 done for you — and done better, because it uses real session resumption rather than
starting cold.

**Barge-in signalling.** `event.interrupted` and `event.turn_complete` come through as
fields on the yielded `Event` (`Event` subclasses `LlmResponse`, so
`llm_response.py:99`'s `interrupted` is on every event you receive).

**Transcripts.** `input_transcription` / `output_transcription` arrive as separate events
carrying `types.Transcription(text=..., finished=...)`, with `event.partial` `True` for
streaming chunks and `False` for the flushed final.

### 3.6 What ADK does *not* give you — and this is the important one

> **ADK does nothing about acoustic echo.**

Searched the entire installed package: no AEC, no half-duplex gating, no self-audio
suppression, no mute-while-speaking. The only echo mitigation anywhere in the
distribution is the browser default from `getUserMedia({audio: true})` in the bundled dev
UI — which does not even pass an explicit `echoCancellation` constraint.

**The §1.2 bug is entirely yours to solve, and it will happen.**

The crude workaround —
`activity_handling=types.ActivityHandling.NO_INTERRUPTION` — makes the model finish its
turn regardless of input. That stops self-interruption *and* genuine user barge-in. It is
not an echo fix; it is turning off conversation.

### 3.7 `[blocker]` Live mode is Gemini-only

```python
# models/base_llm.py:276
def connect(self, llm_request: LlmRequest) -> AbstractAsyncContextManager[BaseLlmConnection]:
    raise NotImplementedError(f'Live connection is not supported for {self.model}.')
```

`grep -n "def connect" models/*.py` returns exactly two hits: that default, and
`google_llm.py:432`. `anthropic_llm.py` and `lite_llm.py` define none.

So a non-Gemini model can **never** run under `Runner.run_live`. If you were planning a
model-agnostic voice layer, ADK's live path is not where you get it. Plan around this
rather than discovering it late.

---

## Part 4 — Choosing between them

| concern | own transcriber (A) | ADK `run_live` (B) |
|---|---|---|
| continuous stream | you build it | built in |
| reconnect | you build it | built in, 5 attempts, real resumption |
| transcription config | manual, `1007` trap | on by default |
| turn-taking | server VAD, callbacks | server VAD or manual signals |
| **echo suppression** | **you build it** | **you build it** |
| model choice | any pipeline you like | **Gemini only** |
| text-in from your own ASR | native | `send_content()` |
| gate what it says before it says it | yes, if you own TTS | no, with native audio |

**The recommendation.** Use B — ADK's live path — as the transport, and keep three pieces
of A that ADK does not provide: the echo gate, the queue bound, and the cumulative-text
rule.

**One exception, and it matters if money is involved.** With native audio, speech *is* the
model's output — by the time you have transcript text to inspect, those bytes are already
playing. You cannot suppress a sentence like "I've already refunded you" after the fact.
If your agent takes consequential actions, run the split pipeline instead:

```
mic → gemini-3.5-transcribe-live-preview → LLM (TEXT out) → TTS → speaker
```

You keep everything inside Google/Vertex, and you get back three things native audio takes
away: text exists before speech (so you can refuse to synthesize a sentence), you own the
input stream (so the echo gate is possible), and you can switch voice/language per
sentence rather than once per session.

---

## Part 5 — The conversational loop

Continuous listening alone gives you a system that hears everything and understands
turn-taking badly. These are the mechanisms that make it feel like a conversation.

### 5.1 The echo gate: substitute silence, never withhold frames

The obvious fix for §1.2 is to stop sending mic frames while speaking. **That is wrong** —
withhold frames and the recognizer's stream times out, resurrecting §1.1.

```python
async def feed_mic(self, pcm16_16k: bytes) -> None:
    # Browser AEC does not reliably cancel Web-Audio playback. Without this
    # gate she transcribes herself, reads it as an interruption, and cuts her
    # own reply after the first sentence.
    if self._speaking or time.monotonic() < self._echo_until:
        # NEVER silence the stream: feeding digital silence keeps the session
        # and its VAD alive and transcribes to nothing.
        pcm16_16k = b"\x00" * len(pcm16_16k)
    self._queue.send_realtime(
        types.Blob(data=pcm16_16k, mime_type="audio/pcm;rate=16000")
    )
```

Plus a tail, because speakers keep ringing after the last chunk:

```python
finally:
    self._speaking = False
    self._echo_until = time.monotonic() + 0.6
```

> **Keep in check:** that `0.6` measures from when the **server** finished sending bytes,
> not from when the **client** finished playing them. If your client buffers deeply, the
> tail must cover the client's queue depth too, or the last sentence still leaks back.

### 5.2 Barge-in belongs on the client

Waiting for a server round-trip before muting means the assistant talks over the user for
a full RTT. So the client decides, then tells the server:

```ts
const BARGE_LEVEL = 0.08;      // AEC-processed mic RMS
const BARGE_SUSTAIN_S = 0.3;   // sustained, to reject coughs and clicks

if (speaking && micOn && rms > BARGE_LEVEL) {
  held += dt;
  if (held > BARGE_SUSTAIN_S && !bargeSent) {
    flushPlayback();                              // stop OUR audio first
    ws.send(JSON.stringify({ type: "barge_in" })); // then tell the server
    bargeSent = true;
  }
} else {
  held = 0;
}
```

Local action first, server reconciliation second.

> **Keep in check:** this pattern is only safe because stopping audio is *reversible*.
> Never apply optimistic-local-then-reconcile to an irreversible side effect.

### 5.3 Cancellation needs a generation counter

A barge-in must cancel work already in flight, including work sitting in a worker thread:

```python
async def speak(sentence: str, gen: int) -> None:
    if not sentence or gen != self._tts_gen:
        return                                  # cancelled before synthesis
    audio = await asyncio.to_thread(synthesize, sentence)
    if gen != self._tts_gen:
        return                                  # cancelled DURING synthesis
    await send_bytes(strip_wav_header(audio))
```

The **second** check is the one people forget. Without it, a sentence whose synthesis was
already running when the user interrupted still reaches their ears.

### 5.4 Chunk at safe boundaries, with one tokenizer

```python
SENTENCE_END = re.compile(r"([.!?।]+[\"')\]]*\s+|\n+)")
```

The mandatory trailing whitespace is load-bearing: without it `3.3`, `claude.ai` and
`₹1,299.50` split mid-token. Speaking a reply sentence-by-sentence as tokens arrive
removes seconds of dead air versus waiting for the full response.

Use the **same** regex for your outbound content guard and your TTS chunker. If a guard
and the thing it guards tokenize differently, that difference is the bypass.

### 5.5 The full turn lifecycle

```
user speaks
  → client: capture at device rate, downsample to PCM16/16k, ~100 ms frames
  → server: echo gate (pass-through when idle)
  → queue.send_realtime(Blob)
  → server VAD: ACTIVITY_START
  → interim transcripts (cumulative)  → UI replaces held text
  → server VAD: ACTIVITY_END
  → final transcript                  → UI settles the bubble, turn enqueued

assistant responds
  → _speaking = True, emit assistant_speaking_start
  → [echo gate now substitutes silence for every mic frame]
  → tokens stream → split on SENTENCE_END → per-sentence guard → TTS → binary frames
  → client schedules each chunk at max(now + 0.03, playHead); playHead += duration
  → turn ends: _speaking = False, _echo_until = now + 0.6, emit assistant_speaking_end
  → [echo gate releases 0.6 s later]

barge-in (any time during the above)
  → client RMS > 0.08 for 0.3 s → flush local playback → send barge_in
  → server: _tts_gen += 1, _speaking = False, emit interrupted, interrupt the model
```

---

## Part 6 — Permission gating on the live path

If your agent can act, this section decides whether the gate works at all.

### 6.1 Which callbacks fire on `run_live`

Verified against the source, not assumed:

| callback | fires on live? |
|---|---|
| `before_tool_callback` / `after_tool_callback` | **yes** — `functions.py:773` |
| `on_tool_error_callback` | **yes** |
| `before_agent_callback` / `after_agent_callback` | **yes** — `base_agent.py` |
| `before_model_callback` / `after_model_callback` | **no** — only in `_call_llm_async`, which `run_live` never calls |

Put your gate on the **tool** callbacks. A gate written as a model callback will silently
never run in live mode.

### 6.2 `[blocker]` ADK's own human-in-the-loop confirmation is broken on live

`ToolContext.request_confirmation` (`context.py:856`) plus the `adk_request_confirmation`
flow is ADK's built-in approval mechanism. On the live path it does not work: in
`_execute_single_function_call_live` (`functions.py:773`), `tool_confirmation` is
hard-coded `None`, with a `TODO` comment at lines 823-826, and the live client never
receives the confirmation function call.

**Build your own approval loop.** Do not design around `request_confirmation` for a voice
agent.

### 6.3 How a `before_tool_callback` actually blocks — and the trap

```python
# agents/llm_agent.py:225
_SingleBeforeToolCallback: TypeAlias = Callable[
    [BaseTool, dict[str, Any], ToolContext],
    Union[Awaitable[Optional[dict[str, Any]]], Optional[dict[str, Any]]],
]
```

Returning **any non-`None` value** becomes the tool result and the real tool is skipped.
`None` lets it run. There is no separate "deny" signal.

Now the trap. Two different checks are applied to your return value:

```python
# functions.py:611-622  — the callback list loop
    for before_callback in agent.canonical_before_tool_callbacks:
        ...
        function_response = callback_result
        if function_response:          # TRUTHINESS
          break

# functions.py:625      — whether the tool runs
    if function_response is None:      # IDENTITY
      function_response = await __call_tool_async(...)
```

So if you register a **list** of callbacks and your deny returns an empty dict `{}`:

- `{}` is falsy → the loop does **not** break → the next callback runs
- that callback returns `None` → `function_response` is reassigned to `None`
- Step 3 sees `None` → **the tool executes**

Your deny is silently overwritten.

> **Rule.** Always return a **non-empty** dict from a deny, and prefer a single callback
> over a list on any path that gates a consequential action.

### 6.4 A working gate

```python
def permission_gate(tool, args, tool_context):
    tier = tier_for(tool.name)                    # data, never prompt text
    if tier is Tier.LOW:
        audit(tool.name, args, "auto")
        return None                               # proceed

    key = approval_key(tool_context, tool.name, args)   # bind the ARGUMENTS
    if key in approved_keys(tool_context):
        audit(tool.name, args, "approved")
        return None

    audit(tool.name, args, "blocked")
    return {                                      # non-empty: this is the deny
        "status": "permission_required",
        "action": tool.name,
        "tier": tier.value,
    }
```

Two things to keep in check:

- **Callbacks receive the same mutable `args` dict** that is handed to the tool.
  In-place rewriting works; rebinding the name does not. Useful for redaction, dangerous
  by accident.
- **Bind the approval to the arguments**, not just the action name. An approval keyed only
  on `(step, action)` authorises that action with *any* arguments on re-run — and after an
  approval the model typically regenerates the call rather than replaying it.
- **Plugin callbacks use the keyword `tool_args`; agent-level callbacks use `args`.**
  Plugins are keyword-only, must be `async def`, and run *before* the agent's own
  callbacks.

---

## Part 7 — Things to keep in check

A checklist distilled from everything above.

**Stream lifecycle**
- [ ] One recognition session spans the whole conversation, not one utterance
- [ ] Reconnect is automatic with exponential backoff; `connect()` returning is not proof of connection
- [ ] The audio queue is bounded and evicts the **oldest**; it is never drained on reconnect
- [ ] `feed()` never blocks and never raises into the caller

**Echo and turn-taking**
- [ ] Mic frames are replaced with **silence** while speaking — never withheld
- [ ] An echo tail covers the client's playback buffer depth, not just server send time
- [ ] Exactly one VAD regime: server-side *or* manual activity signals, never mixed
- [ ] Barge-in acts locally first, then notifies the server
- [ ] Cancellation is checked **after** every `await`, not only before

**Transcripts**
- [ ] The contract states cumulative-vs-delta explicitly, as a protocol-level invariant
- [ ] The client replaces, never appends; an empty frame does not clear the turn
- [ ] The same merge rule covers both directions (user speech and assistant speech)

**ADK specifics**
- [ ] `RunConfig` is constructed per session (`run_live` mutates it)
- [ ] The `LiveRequestQueue` is bounded by you — ADK's is unbounded
- [ ] Gates are on **tool** callbacks; model callbacks never fire on live
- [ ] Deny returns a **non-empty** dict; prefer a single callback over a list
- [ ] Not relying on `request_confirmation` — it is broken on the live path
- [ ] Accepted that live mode is **Gemini-only**

**Observability**
- [ ] Callback exceptions inside the receive loop are logged at `warning`, not `debug`
- [ ] Frame counters are actually surfaced somewhere
- [ ] A degraded path (fallback model, fallback recognizer) is **visible** to the user
- [ ] One goal/session id reconstructs the whole conversation from one place

**Testing** — the lesson that cost the most
- [ ] At least one test drives **real audio** through the socket

Typed-message tests never touch the speech path. Two audio bugs in this project shipped
past a fully green suite for exactly that reason, and the repo still has no voice test
today. Synthesize speech with TTS at 16 kHz LINEAR16, strip the 44-byte WAV header, and
stream it into the websocket as binary frames in 100 ms chunks at realtime pace with
trailing silence for endpointing. That test would have caught both.

---

## Constants reference

Every tunable in one place, with what it protects.

| constant | value | protects |
|---|---|---|
| input sample rate | 16 kHz PCM16 LE mono | recognizer contract |
| output sample rate | 24 kHz PCM16 LE mono | model output; **differs from input on purpose** |
| mic frame size | ~100 ms | latency vs syscall overhead |
| `_MAX_QUEUED_FRAMES` | 1500 (~30 s) | unbounded backlog |
| `_CONNECT_TIMEOUT_S` | 20.0 | a hung connect blocking startup |
| `_BACKOFF_START_S` / `_BACKOFF_MAX_S` | 0.5 → 10.0 | hot-looping against the service |
| echo tail | 0.6 s | speaker ring-out being transcribed |
| `BARGE_LEVEL` | 0.08 RMS | false interrupts from room noise |
| `BARGE_SUSTAIN_S` | 0.3 s | coughs, clicks, door slams |
| playback lead | 0.03 s | scheduling underrun |
| `DEFAULT_MAX_RECONNECT_ATTEMPTS` | 5 (ADK's own) | infinite reconnect loops |

None of these are universal. They are the values that worked on this hardware with this
network, and every one of them should be re-measured on yours — particularly the echo
tail, which is a property of the user's speakers, not of your code.
