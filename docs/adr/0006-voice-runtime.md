# ADR 0006: The voice runtime

Status: accepted, 2026-09-05. Implements specification section 19 (19.1 through 19.15) and
29.6. Sources: `docs/CONTINUOUS_LISTENING_ADK.md` (a field guide written from a real voice
build, cited below as `guide:`) and `PROJECT_SPECIFICATION.md` (`spec:`). Code is
`packages/voice-runtime/src/voice_runtime/...` (`ours:`) and
`apps/buyer-web/src/features/voice/...`.

Numbers in section 4 were measured on this machine against the live services on
2026-09-05. Everything else in this file is a decision, and each says why.

## 0. The one thing that does not move

**Text exists before speech, always.** No `run_live`, no native audio, no ADK tool
confirmation, on any path. With native audio the model's speech *is* its output, so by the
time there is text to inspect the bytes are already playing. This platform says amounts,
payment outcomes and refund status aloud, and "I have already refunded you" cannot be
recalled. The extra hop of latency buys the ability to refuse a sentence before it is
spoken, and that is the trade this project exists to make.

`spec: 19.1` gives four independent reasons and the guide arrives at the same place from
experience (`guide: Part 4`, "One exception, and it matters if money is involved"). They
agree, which is the strongest signal available.

## 1. What was taken from the field guide, and what was changed

### 1.1 Taken unchanged

| Guide | Ours | Why it survived contact |
| --- | --- | --- |
| One recognition session outlives the utterance (`guide: Rule 1`) | `ours: stt/session.py` | A per-turn recognizer loses every word spoken in the gap between turns |
| `connect()` returning is not proof the stream is up | `TranscribeSession.start` waits 20 s, warns, returns | Raising drops the whole voice session for a transient blip, which is exactly when a buyer must not be dropped |
| Evict the **oldest**, never drain on reconnect (`guide: 2.3`) | `ours: stt/queue.py` | Draining was the original bug; dropping the newest discards the speech being produced right now |
| `feed()` never blocks and never raises | `FreshAudioQueue.feed` | The microphone path cannot be allowed to fail |
| `input_audio_transcription` is mandatory | `ours: stt/gemini.py`, with the reason in a comment | Omitting it closes the socket with `1007`, not a validation error. Verified live |
| Transcribe 3.5 serves from `"global"` only | `TRANSCRIBE_LOCATION`, ignores `GOOGLE_CLOUD_LOCATION` | A regional endpoint 404s. Verified live |
| Replace, never append; empty never clears (`guide: Rule 2`) | `apply_stream_text` = `incoming or current` | See 4.3: measured live, the recognizer really does revise |
| Substitute silence, never withhold frames (`guide: 5.1`) | `ours: stt/echo_gate.py` | Withholding starves the recognizer and resurrects the dead-socket failure |
| Generation checked **after** every await (`guide: 5.3`) | `ours: tts/synth.py` | The second check is the one people forget |
| One tokenizer for the guard and the chunker (`guide: 5.4`) | `ours: tts/tokenizer.py`, imported by both | If they tokenize differently, that difference is the bypass |
| Deny returns a **non-empty** dict, single callback not a list (`guide: 6.3`) | Not ours to hold: see 2.1 | |

### 1.2 Changed, and why

**The queue bound is freshness, not thirty seconds.** The guide bounds at ~30 s of frames.
`spec: 19.4` overrides it and is right: a 30 s buffer survives a long reconnect by replaying
half a minute of stale speech into a live commerce conversation, so the buyer watches the
assistant answer a question they abandoned twenty seconds ago and possibly act on a basket
instruction they have since changed. We hold **both** bounds -- a capacity bound so memory
is bounded without consulting a clock, and a freshness bound of 4 s that ages frames out on
the way out. Ageing a stale frame out is not the same act as draining the queue.

**The echo tail starts from the client, not the server.** The guide's 0.6 s is measured from
when the *server* finished sending, and it flags the risk in passing. Section 4.2 shows the
gap is not a rounding error: with sentence-by-sentence synthesis the client can be holding
**8.6 seconds** of queued audio at the moment the server's last byte goes out. A tail
measured from server send time would release the gate while the assistant is still audibly
speaking, and she would transcribe herself. The client reports `playback_ended`; the server
never assumes it, and holds for at most 30 s before releasing with a *visible* degradation.

**Stream rotation, which the guide does not have.** `spec: 19.3`: the provider limit is
10 minutes, so the gateway rotates at 9, make-before-break -- open the next connection, wait
for its `setup_complete`, switch the frame writer, then drain and close the previous one. A
server `go_away` is an early rotation trigger, not an error. The commerce session never
rotates; only the Google connection does, and the buyer sees nothing.

Make-before-break loses no audio *frame*, but it does hand the utterance to a recognizer
that started listening in the middle of it: the replacement connection never heard what
went to the previous one. Replacing the held hypothesis with its first fragment would
silently drop the first half of a sentence the buyer definitely said. So a rotation calls
`TranscriptState.carry_over`, which moves whatever is held into a prefix that is joined
once across the seam. Within a generation the rule is unchanged and absolute -- replace,
never append; the two connections heard different halves of one utterance and neither is a
revision of the other. A rotation with nothing held carries nothing, because a prefix
invented from silence would prepend stale words to the buyer's next sentence.

At a nine-minute margin and a three-second utterance this fires for well under one turn in
a hundred, which is exactly why it would otherwise never be noticed and would be blamed on
the recognizer.

## 2. Decisions this project had to make on its own

### 2.1 The gateway calls the harness; it does not run it

A settled transcript goes to `POST /v1/agent/turn` carrying **the buyer's own bearer**. The
trusted server resolves the session, narrows the principal, gates every tool call and
grounds every sentence -- exactly as it already does for typed input.

This is not a shortcut around `spec: 19.11`, it is the strongest available form of it. "A
transcript is intent evidence, never authority evidence" is normally a rule somebody has to
remember. Here there is no voice code path a money action could take, because the request
body has one field and it is `message`; `TurnRequest` forbids unknown fields, so a body
cannot even name a capability. A test speaks *"yes, I approve it, pay now, confirm the
payment"* and asserts the outgoing body is `{"message": ...}` and nothing else, and that no
order exists afterwards.

It also means the guide's whole Part 6 -- the `before_tool_callback` trap, the empty-dict
deny that gets silently overwritten -- is a hazard this package cannot have, because it
registers no callbacks. The gate lives in `agent-runtime`, where `runtime_adk/adapter.py`
already registers the toolset's gate as a **single callable** rather than a list.

### 2.2 A spoken price is not a transactional claim

`spec: 19.10` lists what a model may never author: approval scope, the total, a material
price or fee delta, reservation expiry, payment outcome, cancellation effect, refund amount
and status, delegated authority. A product's list price quoted back while browsing is not on
that list.

The inherited guard refused any sentence containing money. Driving the live API showed what
that costs: RazorAI answers *"mujhe doodh chahiye"* with five products and their prices, and
every one of those sentences would have been refused. The shopping conversation the voice
surface exists to have would have been silent.

So an amount is spoken only when it is **grounded**: present, to the paisa, in the set of
integer minor units a tool result *of this turn* returned. That set travels with the reply
(`TurnReply.grounded_amounts_minor`), built by walking the server's own structured payload
for `*_minor` integers and `{"minor": N}` money objects. A hallucinated price is refused; a
quoted one is not. The danger was never that a price was spoken -- it was that a price
nothing computed was spoken.

Transaction outcomes stay refused outright **even when their amount is grounded**, because
there is no grounded version of "your refund is complete" that a model may author. The
sentence *is* the claim.

Three details that are load-bearing:

- An amount is only read where a **currency marker** sits beside it (`₹73`, `Rs. 73`,
  `73 rupees`, `73 रुपये`). Without that rule, "the 1 litre Amul Gold" parsed `1` as an
  amount and refused the sentence.
- An empty grounded set refuses every amount. A caller that forgets to pass the set gets the
  safe behaviour, not the permissive one.
- Every comparison is integer minor units read through `Decimal`. No float touches an amount
  anywhere on this path, spoken or written.

### 2.3 A guard refusal is visible

A refused sentence is silence where a sentence should have been, and silence the buyer
cannot account for is indistinguishable from a bug. The text is already on screen -- the
whole point of the split -- so the pipeline emits a `speech_guard_refused` degradation
saying so. `spec: 19.12`: silent degradation is a defect, and that includes silence we chose.

### 2.4 Staleness measures the audio, then the queue

The inherited freshness check stamped a transcript with `now` and compared it to `now`, so
it could never fail. Two fixes:

1. A stamp carries `audio_age_s` -- how old the microphone frame already was when the
   recognizer received it -- and that is what the window is compared against.
2. `TRANSCRIPT_FRESHNESS_S` is an **end-to-end budget of 12 s**, deliberately larger than
   the 4 s audio window. Equal to it, the check is unreachable: the queue already refuses to
   send audio older than 4 s.

What the queue cannot see is a turn that settled while a previous turn was still running and
then waited. So the check runs where a turn can actually have waited -- after the turn lock
-- and a turn that aged out there is dropped with a `stale_turn_dropped` degradation. A
buyer left waiting for an answer that will never come is worse than being asked to repeat
themselves.

### 2.5 The socket is opened with a ticket, not a bearer

A browser cannot put an `Authorization` header on a WebSocket handshake. The alternatives
are the bearer in the query string -- where it lands in access logs, proxy logs and
`Referer` headers -- or an opaque handle. `ours: wire/tickets.py` mints 32 random bytes,
60 second lifetime, consumed on first redeem, with the bearer held server-side behind it. A
spent ticket reports `unknown` rather than `consumed`, so replay and guess are
indistinguishable and neither confirms a ticket ever existed. `TicketClaims.__repr__`
redacts the bearer, because a bearer that reaches a traceback has already leaked.

Identity is re-resolved when the socket opens rather than trusted from the minted copy: a
ticket can be a minute old and a session can be revoked inside a minute.

Origin is checked on the handshake against an exact allow-list. Missing and `null` are
refused: this is a browser surface, and browsers always send Origin.

### 2.6 One recognizer, one voice regime

`spec: 19.11`: exactly one voice-activity regime per session. We use **server-side detection
only** and never send `activity_start` / `activity_end`. ADK performs no validation against
mixing them and the server rejects mixed signals, so the safe construction is to have no
code that could send the other kind.

### 2.7 Where the spec and the guide were both silent

Per the standing instruction, the option giving speech **less** authority was taken:

- **Push-to-talk, not open-mic.** Continuous listening is built and works; the storefront
  gates transmission behind a held control. A microphone that is always on is a microphone
  that can be acted on when nobody addressed it.
- **A voice turn never carries page context.** The turn body is the message alone -- no
  `checkout_id`, no `basket_id` -- even though the typed endpoint accepts them. A spoken
  sentence should not be able to aim itself at a checkout the buyer has not named.
- **Only finals leave the gateway.** Interim text never reaches the agent, so 19.5's rule is
  enforced by there being no call to make, not by a flag the server must honour.
- **`playback_ended` is not sent after a barge-in.** That frame starts the echo tail, which
  substitutes silence for microphone frames -- and a barge-in is precisely when the buyer
  *is* speaking. The server learns the speakers stopped from `barge_in` itself.
- **The merchant console has no voice surface.** A merchant session is refused a ticket.

## 3. Shape

```
mic ──► push-to-talk gate ──► 16 kHz PCM16 frames ──► WebSocket (binary)
                                                          │
   ┌──────────────────────────────────────────────────────┘
   ▼
echo gate ──► fresh queue ──► Transcribe Live ──► interim (replace) ──► client
                                     │
                                     └──► FINAL ──► POST /v1/agent/turn  (buyer's bearer)
                                                          │
                                     ┌────────────────────┘
                                     ▼
                     deterministic template  OR  guarded model text
                                     │
                     agent_reply (TEXT) ──► client        ◄── on screen first, always
                                     │
                     sentence chunker ──► guard ──► TTS ──► 24 kHz PCM16 ──► client
```

Every Google call and the transport sit behind Protocols, so the object graph a test drives
is the object graph that serves a browser. Only three things are swapped: the transport, the
recognizer and the synthesiser.

## 4. Measured, not assumed

All figures from this machine, 2026-09-05, against Vertex at location `global` and the
commerce API on `127.0.0.1:8000`. `spec: 19.15` says to re-measure per deployment; this is
that measurement, and it should be redone on the demo hardware.

### 4.1 Both Google surfaces, exercised

| Surface | Result |
| --- | --- |
| `gemini-3.5-transcribe-live-preview` @ `global` | connects; accepts `audio/pcm;rate=16000` |
| `gemini-3.1-flash-tts-preview` @ `global`, voice `Kore` | 24 kHz PCM16, mime `audio/l16; rate=24000; channels=1` |
| `gemini-2.5-flash-tts` @ `global` (fallback) | 24 kHz PCM16, mime `audio/L16;codec=pcm;rate=24000` |
| Cloud TTS Chirp 3 HD | **403 SERVICE_DISABLED** on this project |

Two findings worth carrying:

- **Gemini TTS returns raw PCM, not a WAV container.** `strip_wav_header` still guards the
  path and returns non-RIFF input unchanged, and the header is parsed rather than assumed to
  be 44 bytes -- a `LIST` chunk before `data` would otherwise be played as a click.
- **The two pins spell the same mime type differently.** The sample rate is parsed out of
  the string rather than the string matched, because one of them is `l16` and the other is
  `L16;codec=pcm`.

Chirp being unavailable is why the synthesiser is a fallback chain that reports which link
spoke. A quieter, different voice with no explanation is a silent degradation. See
`docs/briefs/REQUESTS_TO_CLAUDE.md` item 5.

### 4.2 Latency, and where the echo tail really has to reach

Synthesising a three-sentence reply, per sentence:

| | s1 | s2 | s3 | total |
| --- | --- | --- | --- | --- |
| synthesis latency | 4.27 s | 2.45 s | 3.18 s | 9.90 s |
| audio duration | 4.08 s | 2.72 s | 1.80 s | 8.60 s |

- **Sentence chunking removes 5.6 s of dead air.** First audio at 4.27 s instead of 9.90 s.
  That is `spec: 19.9`'s claim, measured.
- **The client can hold 8.60 s of queued audio** when the server's last byte goes out. This
  is the number that decides the echo gate. A 0.6 s tail measured from *server send time*
  would release the gate roughly eight seconds before the assistant stops being audible, and
  she would transcribe herself -- the guide's failure 1.2, reintroduced by a constant that
  looked right. The tail is measured from the client's `playback_ended` report, and
  `ECHO_GATE_MAX_HOLD_S = 30 s` comfortably covers a reply of this length if the report is
  lost.

### 4.2b Time to first audio, and why the chunker is not enough

Speaking to the running gateway with real audio, RazorAI answers a product search with a
single **350-character** sentence listing five products and their prices. Sentence
chunking cannot help: there is no sentence boundary inside it. Three measured runs of the
same utterance, each change kept:

| | time to first audio | whole reply delivered |
| --- | --- | --- |
| sentence chunking only | 21.1 s | 39.9 s |
| + phrase splitting at commas | 13.4 s | 39.9 s |
| + synthesis pipelined two ahead | **8.3 s** | **20.4 s** |

Both changes are in `ours: tts/synth.py` and `ours: tts/tokenizer.py`:

- **Phrase splitting** cuts an already-approved sentence at `, ` and `; `. It cannot cut an
  amount, because Indian digit grouping never puts a space after its comma. It makes the
  speaker's unit smaller than the guard's and never the reverse, so 19.9 holds.
- **Pipelining** synthesises up to two phrases ahead of the one being sent. Synthesis is
  slower than playback here, so doing them strictly in turn left the buyer in silence for
  the *sum* of the two rather than the larger. Chunks are still sent in order, and the
  generation is re-checked after every await, so a barge-in still cancels work in flight
  and the look-ahead with it.

The second row is the one that matters for whether it *sounds* right: before pipelining,
playback began at 19.9 s with 17.3 s of audio and the next chunk did not arrive until
39.9 s -- the buyer would have heard the assistant stop mid-list and resume. After, the
whole 36 s of audio is delivered by 20.4 s and playback never starves.

**Read those absolute numbers with suspicion.** They are single runs, and Gemini TTS
latency varies a great deal: a later run of the identical utterance on the identical code
took 17.9 s to first audio rather than 8.3 s, and produced 59.8 s of audio for the same
reply that had produced 36.2 s. The service, not the pipeline. What is reliable is the
*comparison* -- the three rows are consecutive runs of one utterance with one change
between each -- and the ordering property, which is structural rather than timing-based:
after pipelining the whole reply is delivered before playback of the first chunk could
finish, so it cannot starve regardless of how slow synthesis happens to be that minute.

This variance is itself a reason the reply should be shorter. A 350-character sentence is
exposed to it; three short ones are not.

**8.3 s is still not conversational, and the remaining cost is not in this package.** The
reply is written for a screen: five products, full names, prices, in one sentence. A voice
register -- "I found five milks. Amul Gold one litre is 73 rupees. Want me to add one?" --
is a prompt change in `agent-runtime`, not a chunking change here. Recorded in
`REQUESTS_TO_CLAUDE.md`.

### 4.3 Recognition, and live evidence for the replace rule

Three runs each, at realtime pace:

| | first partial after speech starts | final after last speech frame |
| --- | --- | --- |
| en-IN, 2.08 s of speech | 1.59 s median | 1.27 s median, 1.29 s max |
| hi-IN, 2.60 s of speech | 1.51 s median | 1.46 s median, **1.47 s max** |

`ENDPOINTING_SILENCE_S = 1.5 s` in the test harness comes from that 1.47 s worst case, not
from a guess. `MAX_FRESH_AUDIO_AGE_S = 4 s` comfortably covers a normal utterance plus its
endpointing.

The interim sequence for `मुझे दो लीटर दूध चाहिए`:

```
मुझे  ->  मुझे 2 लीटर  ->  मुझे 2 लीटर दूध  ->  मुझे 2 लीटर दूध चाहिए।
FINAL: मुझे दो लीटर दूध चाहिए।
```

The final **revises** `2` to `दो`. It is not an extension of the last interim, and a client
that appended would have produced `मुझे 2 लीटर दूध चाहिए।मुझे दो लीटर दूध चाहिए।`. The
English clip revises too: `2 L` -> `two liters of milk` -> `Two litres of milk, please.`
This is the guide's failure 1.3 reproduced live, and the whole justification for
`applyStreamText(current, incoming) = incoming || current`.

### 4.4 Constants, and what each protects

Values in `ours: constants.py`. Changed from the guide's table where measurement or the
specification said so.

| Constant | Value | Source | Protects |
| --- | --- | --- | --- |
| input rate | 16 kHz PCM16 LE mono | verified live | recognizer contract |
| output rate | 24 kHz PCM16 LE mono | verified live | model output; differs on purpose |
| `MIC_FRAME_MS` | 100 | guide | latency against syscall overhead |
| `MAX_SYNTHESIS_CHARS` | 110 | §4.2b | time to first audio on a long sentence |
| `SYNTHESIS_LOOKAHEAD` | 2 | §4.2b | playback starving between phrases |
| `MAX_FRESH_AUDIO_AGE_S` | 4.0 | **spec, not guide's 30 s** | stale speech replayed after a reconnect |
| `QUEUE_MAX_FRAMES` | 50 (~5 s) | derived | unbounded backlog, without consulting a clock |
| `TRANSCRIPT_FRESHNESS_S` | 12.0 | **raised from 4.0** | a turn queued behind a long turn (2.4) |
| `STREAM_ROTATION_MARGIN_S` | 540 (9 min) | spec | the 10 min provider limit |
| `ROTATION_TICK_S` | 0.25 | chosen | how late a rotation may be; testable on a fake clock |
| `CONNECT_TIMEOUT_S` | 20.0 | guide | a hung connect blocking startup |
| backoff | 0.5 s -> 10 s | guide | hot-looping against the service |
| `MAX_RECONNECT_ATTEMPTS` | 5 | guide | infinite reconnect |
| `ECHO_TAIL_S` | 0.6, **from client playback end** | guide value, our anchor | speaker ring-out |
| `ECHO_GATE_MAX_HOLD_S` | 30.0 | §4.2 | a lost `playback_ended` muting the buyer forever |
| `BARGE_IN_LEVEL_RMS` | 0.08 | guide | false interrupts from room noise |
| `BARGE_IN_SUSTAIN_S` | 0.3 | guide | coughs, clicks, door slams |
| `PLAYBACK_LEAD_S` | 0.03 | guide | scheduling underrun |
| `VOICE_TICKET_TTL_S` | 60, single use | spec | session and tenant binding |

**`ECHO_TAIL_S` is the one still owed a measurement.** 0.6 s covers the client's report
reaching the server plus speaker ring-out, and the anchor (client playback end, not server
send) is the part that mattered and is fixed. But true acoustic ring-out is a property of
the listener's speakers and needs a physical microphone in the room with them, which this
environment does not have. **Re-measure on the demo machine**: play a sentence at demo
volume, watch how long after `playback_ended` the recognizer still returns text, and set the
tail above that. Every tunable is sent to the client on `session_ready` as a **required**
field, so raising it needs no client release -- and a client that quietly substituted its
own guess would be hardcoding a number measured on somebody else's speakers, which is why
those fields carry no defaults and a missing one fails to parse.

## 5. Tests

`packages/voice-runtime/tests`, 184 offline plus 7 live. Every required case in
`spec: 19.14` has a test.

**At least one test drives real audio through the socket** (`spec: 19.14`,
`test_voice_real_audio.py`, marked `voice_live`, deselected by default). It synthesises
speech, resamples 24 kHz -> 16 kHz, streams it as 100 ms binary frames at realtime pace with
trailing silence for endpointing, and reads the frames that come back. Run it with:

```bash
set -a && . ./.env && set +a && uv run --no-sync python -m pytest packages/voice-runtime -o addopts="" -m voice_live
```

Typed-message tests never touch the speech path; two audio defects in the source project
shipped past a fully green suite for exactly that reason. The live set covers the format
contract, a real final transcript in English and Hindi, real interim revision, the echo gate
with real assistant audio played into the microphone, the output contract, a full
speech-to-RazorAI-to-speech turn against the running API, and the spoken-approval refusal.

Two tests worth naming:

- **`test_every_degradation_kind_exists_on_both_sides`** (`test_voice_wire_contract.py`).
  The wire contract has two implementations and the client parses strictly, so a frame kind
  the client has never heard of is dropped **silently** -- a visible degradation turned
  invisible. This happened during this work: `speech_guard_refused` and `stale_turn_dropped`
  were added server-side after the client was written, and nothing failed. The test reads
  the TypeScript as text rather than executing it, so it cannot be skipped for want of Node.
- **`test_a_sentence_cancelled_during_synthesis_never_reaches_the_speaker`** parks synthesis
  mid-flight, barges in, then lets synthesis complete, and asserts nothing was sent. That is
  the check after the await, which is the one that gets forgotten.

## 5.1 Running it

```bash
set -a && . ./.env && set +a
export VOICE_GATEWAY_API_BASE_URL=http://127.0.0.1:8000
export VOICE_GATEWAY_ALLOWED_ORIGINS=http://localhost:3000
uv run --no-sync python -m voice_runtime.gateway     # listens on 127.0.0.1:8100
```

`GET /healthz` answers `{"status":"ok","speech_available":true}` once Vertex is
configured; `speech_available:false` means the socket will open in text mode and say so
rather than listening to a microphone it cannot transcribe.

Then, as a browser would: `POST /v1/voice/tickets` with the buyer's bearer, and open
`ws://…/v1/voice/stream?ticket=…` with an `Origin` header on the allow-list. Send binary
frames of PCM16 LE mono at 16 kHz, ~100 ms each. `GET /v1/voice/metrics` surfaces the
counters of 19.13.

Nothing serves that socket to the storefront yet -- see gap 1.

## 6. Known gaps

1. **Nothing serves the WebSocket to the browser yet.** The gateway is an ASGI app; the
   storefront expects a same-origin `/api/voice/stream`. Two Next routes are needed, in a
   session that owns `apps/buyer-web/src/app/api/`. See `REQUESTS_TO_CLAUDE.md` item 1.
2. **Chirp 3 HD is unavailable**, so transactional sentences are currently spoken by the
   conversational voice, with the substitution surfaced. Item 5.
3. **The AudioWorklet may be blocked by CSP**, falling back to the deprecated
   `ScriptProcessorNode` on the main thread. Item 2.
4. **The echo tail is inherited, not measured.** Section 4.4.
5. **The gateway's ticket store is in-process.** A second gateway process needs a shared
   store, and the failure mode of getting that wrong is a ticket redeemable twice.
6. **Voice is INR-only.** A second currency needs a decision about how it is spoken, not
   just a different exponent.
7. **A spoken quantity is lost before it reaches the basket.** "Add two of those" proposes
   a quantity of one, because the quantity is parsed as digits and nobody speaks digits.
   The fix is number words in `commerce-api`, not here: rewriting the buyer's words before
   the agent sees them is the quiet interpretation this whole architecture avoids.
   `REQUESTS_TO_CLAUDE.md` item 7a.
8. **The reply is written for a screen.** Time to first audio is 8.3 s and most of what
   remains is prose length, not pipeline latency. Item 6.
