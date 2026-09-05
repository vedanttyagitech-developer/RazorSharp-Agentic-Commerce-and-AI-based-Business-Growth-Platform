> # SUPERSEDED — use `GEMINI_BRIEF_7.md`
>
> This brief asked for pitch storyboarding, an accessibility audit and a voice panel. The
> first two are design-judgment work rather than the high-volume content and UI work Gemini
> is fast at, and the voice panel is blocked on unfinished code. Brief 7 assigns the five
> prompts, which are blocking Claude right now, a catalogue deep enough to look like a real
> store, and the agent panel on the live endpoint.

# Brief 6 for Gemini — the submission itself

Run brief 5's priority 1 (the five prompts) first: Claude's loader is waiting on those exact
filenames. Everything else here outranks the rest of brief 5, because the submission is due
today and these are the parts a judge actually meets.

---

## STEP ZERO — every session

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
pwd && git branch --show-current
git merge --ff-only main
```

`pwd` must be that worktree, branch `gemini/catalogue`. If it shows the integration tree
`/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay`, stop and change directory.

## What changed since brief 5

**Copilots are harnesses, not agents.** A copilot is now ordinary Python that owns the
session, tenant, locale, correlation identifiers, the transcript, the routing decision and
the binding of a principal to its tools. It calls no model and has no prompt. Only the five
specialists are models.

```
RazorAI     HARNESS (Python)        Merchant Copilot  HARNESS (Python)
├── Shopping Specialist   (agent)         ├── Growth Specialist  (agent)
├── Checkout Specialist   (agent)         └── Case Specialist    (agent)
└── Support Specialist    (agent)
```

So **five prompt files, not seven**: `shopping_specialist.md`, `checkout_specialist.md`,
`support_specialist.md`, `growth_specialist.md`, `case_specialist.md`.

Claude is building the harnesses, the five specialists, the capability gate, the grounding
defences and `POST /v1/agent/turn`. That endpoint returns `specialist` and `routing_reason`
alongside the reply, so your panel can show which specialist answered and why it was chosen.
That is a good thing to show a judge: the routing is deterministic and inspectable, not a
model guessing.

## Priority 1 — the pitch video is the submission

The application asks for a five-minute video. Right now nothing in the repository helps
record one beyond `docs/DEMO.md`, which is Claude's and is a runbook rather than a
storyboard.

Build the recording surface:

- **A demo mode that makes the story legible on camera.** Larger type, slower transitions,
  and a visible step indicator so a viewer following at 1080p knows where they are in the
  eleven steps. A query parameter or an environment flag is fine; it must not change any
  behaviour, only presentation.
- **The refusal moment deserves a beat.** When the kernel refuses a stale approval, the
  screen should hold: the old total and the new one side by side, every delta, and version N
  struck through with N+1 offered. That single screen is the whole argument of the project.
  Design it as though it were the only frame a judge remembers, because it might be.
- **A visible evidence panel** during payment: the proof-chain links appearing one by one as
  they are created, and the audit chain reporting intact. Watching evidence accumulate is
  more persuasive than being told it exists.
- Screenshots for the README at 2x, from the live app, not the mock, once the API is
  running.

## Priority 2 — the storefront against the live API

Still outstanding from brief 3, and it is the difference between a demo and a video of a
mock.

`make bootstrap && make seed && make demo` starts the database, seeds a tenant and runs the
API and worker. Then start the storefront with `NEXT_PUBLIC_API_MODE=live` and
`NEXT_PUBLIC_API_BASE=http://localhost:8000`. Read `docs/DEMO.md` first.

Walk all eleven steps in the browser against the real server and fix what breaks. Expect
real problems: a live backend is slower than a mock, returns RFC 9457 problem documents
rather than thrown errors, and produces states the mock never emitted — `EXECUTION_PENDING`
while the worker creates the Razorpay order, `PAYMENT_UNKNOWN` during reconciliation,
`RECONCILING`, `STALE_CAPTURE`.

Say in your report **which screens you actually drove against the live server** and which
are still only proven against the mock. Getting that distinction wrong is what turns a
recording session into a debugging session.

## Priority 3 — the voice panel, honestly scoped

Specification 19 describes a split pipeline: microphone to transcription, text to the agent,
and a deterministic template to speech, so nothing a model invents is ever spoken as a money
fact. The runtime for that exists but is unfinished and its gateway is a stub.

Build the **surface** and be honest about what is behind it:

- The transcript contract from 19.5: partial and final transcripts, freshness, degradation
  events.
- A push-to-talk control that degrades visibly to text when speech is unavailable, which it
  currently is.
- The rule made visible: money sentences come from templates. Show the buyer that the
  spoken confirmation is generated deterministically rather than by the model.

Do not fake a working microphone. A clearly-labelled unavailable state is better than a
demonstration that fails live.

## Priority 4 — accessibility

Specification 29.8, still not done: keyboard navigation through the whole purchase journey
without a mouse, `aria-live` on the state changes a buyer must notice, visible focus rings,
colour never the only signal, correct heading order. Run an audit and fix what it finds.

## Boundaries, unchanged

Yours: `apps/buyer-web/src/{app,components,features}/**` except `app/api/**`,
`apps/buyer-web/public/**`, `apps/merchant-console/**`, `agent_runtime/prompts/**`, and
`merchant_sim`'s `catalogue.py`, `search.py`, `textfold.py`, `grounding.py`.

Not yours: everything else, and specifically the whole of `agent-runtime` except
`prompts/` — Claude is writing `harness/`, `agents/`, `capabilities/`, `grounding/`,
`backends/`, `rendering/` and `turn.py` right now, so those files change under you. Also
`packages/transaction-kernel`, `platform-db`, `payment-adapters`, `durable-work`,
`commerce-api`, `durable-worker`, `commerce-domain`; `merchant_sim`'s `fees.py`,
`policy.py`, `store.py`, `kernel_adapter.py`, `injection.py`;
`lib/api/{client,types,problem}.ts`; `lib/security/**`; `conftest.py`, `pyproject.toml`,
`uv.lock`, `.github/**`, `infra/**`, `docs/adr/**`, `PROJECT_SPECIFICATION.md`,
`docs/DEMO.md`, `docs/STATUS.md`.

Anything you need changed in a file you do not own goes in
`docs/briefs/REQUESTS_TO_CLAUDE.md`.

## Gate

```bash
cd apps/buyer-web && npm run lint && npm run typecheck && npm run test && npm run build && npm run e2e
cd ../merchant-console && npm run lint && npm run build
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync python -m pytest packages/merchant-sim -o addopts="" -q
```

## Reporting

Append to `docs/briefs/GEMINI_REPORT.md` with real command output. Two things matter most
today: which screens ran against the live API, and whether the refusal moment looks good
enough to be the frame a judge remembers.
