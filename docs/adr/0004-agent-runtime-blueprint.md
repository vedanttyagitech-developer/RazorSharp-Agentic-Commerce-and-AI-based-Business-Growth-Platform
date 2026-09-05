# ADR 0004: The agent runtime, adapted from anthropics/commerce-agents

Status: accepted, 2026-09-05. Revised 2026-09-05 to strike file-ownership annotations that
described a two-author split that no longer exists; no decision was changed, and every
reference to Gemini as a *model runtime* below is deliberate and load-bearing. Extends
specification sections 5.3, 5.4, 6, 20, 29.4 and
`docs/briefs/AGENT_ROSTER.md`. Where this file and `packages/agent-runtime` as it stands
today disagree, this file is what is built.

Reference: anthropics/commerce-agents (Apache-2.0), read at the paths named below as
`ref:`. Ours are `packages/agent-runtime/src/agent_runtime/...`, named `ours:`. Patterns
are adopted; code is written fresh. A transcribed non-trivial block carries a source
comment naming the reference file.

## 0. The two things that do not move

1. **A copilot is a harness.** `harness/` is plain Python with no model and no prompt. It
   owns session, tenant, locale, modality, correlation, transcript, routing, and the
   binding of a principal to a tool set. The reference lets a model route
   (`ref: commerce_common/delegation.py`, `agent.yaml` sub-agents); we do not.
2. **The kernel is the authority.** `ours: backends/base.py` has no approve, pay, refund,
   revoke, cancel or capture method, and `NEVER_ON_AGENT_SURFACE` proves it. The reference
   reaches the same place for payment only (`ref: shopping_agent/backend.py`, "No method
   places an order"); we hold it for every money verb.

## 1. Adopt, pattern by pattern

### 1.1 Fencing: a source-literal label and the real carriers of hidden text

Reference (`ref: commerce_common/fencing.py`). One frozen `Fence(label, notice)` per role;
the label is a literal in source, never built from runtime values. `sanitize_text` does
NFKC, strips 14 ranges of invisible and format controls (zero-width, bidi
embeddings/isolates, tag characters U+E0000-E007F that spell invisible ASCII, variation
selectors), replaces C0/C1 controls, removes copies of the fence marker and transcript /
tool-call tags *to a fixpoint* so a nested marker cannot reassemble, and defuses forged
turn boundaries (`\n\nassistant:`). `fence_payload` sanitizes dict keys and leaves and
stringifies any other object through the sanitizer so a `__str__` cannot carry a marker.

Ours (`ours: grounding/fence.py`). Global `<<<BEGIN_UNTRUSTED_MERCHANT_DATA>>>` markers,
a narrower invisible set (no tag characters, no variation selectors, no isolates), one
pass of marker elision (not a fixpoint), a blunt `<[^>]{0,200}>` markup strip, and a
400-char cap. What ours has that theirs lacks: a **detection** pass (`scan`) that names
instruction-like patterns and **withholds** the string (spec 20.3 quarantine), recorded on
the turn without copying the payload.

Decision: rewrite `ours: grounding/fence.py` around a `Fence` dataclass with the
reference's character ranges, fixpoint marker removal, turn-marker defusing and keyed
`sanitize_value`. Keep our `scan`/withhold as the second line and keep the flag record.
One fence per surface: `MERCHANT_DATA_FENCE` (label `merchant_data`, buyer side) and
`OPERATOR_DATA_FENCE` (label `operator_data`, merchant side; buyer messages and pasted
text are third-party there). The fence notice is one paragraph the prompt loader
injects; the prompt files may not restate the label.

### 1.2 Grounding rules: prevention, not only detection

Reference (`ref: commerce_common/grounding.py`, `shopping_agent/grounding.py`). A
`GroundingRule(name, tool, fires, prefetch_intro)` reads the user's text and names the
one read tool the turn must start with. Rules are in precedence order; lexicons are
config; `first_forced_tool` picks the first that fires. Two enforcement forms: force the
tool (`tool_choice`) or prefetch it and put the fenced result above the message.
`ref: tests/test_grounding.py` pins whole-word matching, term-and-cue, numeric literals
standing in for a term, longest-token id matching, and rule precedence.

Ours. Nothing before the model; `ours: grounding/postcheck.py` catches an ungrounded SKU
or amount *after* the reply and drops the sentence. Detection only.

Decision: add `ours: grounding/rules.py` with the same three primitives and dataclass;
per-specialist rule tuples live beside each specialist. Enforcement on ADK is section 3;
`postcheck.py` stays as the third line. Rules for P0:

| Specialist | Rule | Fires on | Starts with |
| --- | --- | --- | --- |
| shopping | catalogue | an unseen `[A-Z]{2,6}-[A-Z]{2,8}-\d{2,4}` token | `product(sku)` (prefetch) |
| shopping | basket | a quantity or price question with a basket in session | `basket_get` (prefetch) |
| checkout | state | any turn with a checkout in session | `checkout_get` (prefetch) |
| checkout | delivery-or-fee terms | terms and a cue | `basket_get` (forced) |
| support | order | an order id or an order cue | `order_track` (prefetch) |
| support | remedy | refund/cancel terms and a cue | `resolution_evaluate` (forced) |
| growth | metrics | a performance question | `checkout_metrics_read` (forced) |

### 1.3 The provenance gate, caps and write serialization

Reference (`ref: shopping_agent/gates.py`, `commerce_common/types.py::remember`). A cart
write accepts only a product id a catalogue or order tool returned **this session** (or a
line already in the cart); the record is a session provenance map capped at 200 newest
entries. An add of a family with unchosen options is held and pointed at its variants.
The per-item quantity cap applies to the line *after* the write; the line count is
capped; a held call is a normal result with `status: blocked` and the gate's name. Writes
for one session take an `asyncio.Lock` because one round's tool calls run concurrently.
`ref: merchant_agent/gates.py` does the same for listing, campaign and change ids, needs a
full `get_listing` read before a content edit, and re-runs guardrails at apply.

Ours. `basket_set_line` (`ours: capabilities/tools.py`) bounds quantity 0-50 and relies on
the backend's `unknown_sku` problem (spec 20.4). The turn `GroundingLedger` records what
was returned but nothing reads it before a write. No lock. No line cap.

Decision: add `ours: core/provenance.py` (`SessionProvenance`: seen SKUs, seen checkout
ids, seen order ids, seen proposal ids; `remember` with the 200 cap; persisted in ADK
session state under one key) and `ours: core/gates.py` (pure functions returning a
`Held` outcome or None). The tool closures in `capabilities/tools.py` call the gates
before the backend and take the session lock around read-compute-write. The provenance
map is per **session**; the money ledger stays per **turn** (section 2.4).

### 1.4 The core/runtime split

Reference: `shopping-agent/core/` is runtime-agnostic (types, backend, gates, grounding,
prompt, fencing); `runtime-agent-sdk/shopping_agent_sdk/agent.py` is 114 lines that build
`ClaudeAgentOptions` from core and ground the message before `client.query`.
`ref: tests/test_consumption_paths.py` pins that all three paths register the same
registry bytes and return byte-identical tool results.

Ours: `capabilities/tools.py` imports `google.adk` directly, so the gates, payload
builders and ADK are one module. There is no adapter to swap and nothing to compare.

Decision: `core/`, `grounding/`, `rendering/`, `backends/`, `specialists/` and `harness/`
import nothing from `google.adk` or `google.genai`; a source test enforces it.
`runtime_adk/` is the one thin adapter (section 5). `capabilities/tools.py` builds
runtime-neutral `ToolSpec`s; `runtime_adk/tools.py` wraps each in a `FunctionTool`.

### 1.5 Prompt assembly: a stable static half and a fenced dynamic half

Reference (`ref: commerce_common/prompt_assembly.py`, `shopping_agent/prompt.py`). The
static text depends only on deployment config and is byte-identical across turns; every
per-request fact (cart, page, clock rounded to the hour, memory) is a second block, wrapped
in the fence, after the cache breakpoint. The clock is rounded so the block does not
change every minute.

Ours: no prompt assembly existed when this was written, and the roster assigned prompt
text to a file rather than to code. Decision:
`ours: core/prompt.py` with `load_prompt(name)` reading
`agent_runtime/prompts/<name>.md` (optional YAML frontmatter `name`,
`version`) and falling back to a built-in minimal instruction when the file is absent or
malformed, and `dynamic_context(...)` rendering session facts (basket id, checkout id and
version, language label, hour clock) inside the fence. The adapter passes the static half
as `LlmAgent.instruction` and appends the dynamic half to the **user** content of each
turn, never to the instruction (spec 20.1: retrieved content never enters the instruction
block). No `cache_control` exists on Gemini; the discipline is kept for implicit caching.

### 1.6 Turn loop

Reference (`ref: commerce_common/turn.py`). What transfers is not the streaming machinery
(Anthropic-specific) but the invariants: `latest_user_text` ignores host-appended
messages; a tool exception never ends the turn; an interrupted round gets a synthetic
error result so the stored conversation stays valid; `max_tool_iterations` then a round
without tools; one INFO log line per model call carrying a session **tag** (SHA-256
prefix), never the session id; provenance lives on session state so history compaction
clears nothing a gate reads.

Ours (`ours: turn.py`): `TurnContext` is the per-turn evidence record with a tool budget
counted in the gate. Good; it has no round cap, no timeout, no model-call log.

Decision: keep `TurnContext`, move it to `core/turn.py`, add `rounds`, `started`,
`session_tag`. `runtime_adk/turn_runner.py` drives `Runner.run_async` under
`asyncio.timeout`, counts model rounds in `before_model_callback`, and on the last
allowed round sets `FunctionCallingConfigMode.NONE`. A timeout or exception yields
`render_fallback` and leaves state untouched (spec 6.1 fallback; 29.4 "model timeout
leaves transaction unchanged").

### 1.7 Presentation separated from decision

Reference (`ref: commerce_common/presentation.py`). The model *selects* a component and
names ids; the server validates the payload with pydantic, joins every fact from
session records, drops ids without provenance and reports them, refuses a component with
nothing left. Chips are sanitized and capped at four. Result text for the model and a
`ui` event for the host are two outputs of one call.

Ours (`ours: rendering/messages.py`, `money.py`): deterministic templates for every
recovery code in three languages, delta rendering that cannot skip a delta, Indian
grouping by integer slicing. Stronger than theirs on money; nothing for cards.

Decision: keep `rendering/` as is and add `rendering/cards.py`: `ProductCard`,
`BasketCard`, `ApprovalCard`, `DecisionCard`, `PlanCard` payloads built **only** from
structured tool results already in the turn ledger, and `present_*` tools (one per card,
through the factory) whose only arguments are ids; a present call with an id the ledger
does not know is held by the provenance gate. Chips: `sanitize_label`, at most four.

### 1.8 Skills

Reference (`ref: commerce_common/skills.py`): `SKILL.md` directories, an index in the
static prompt, `load_skill` returns a body on demand. Decision: adopt the loader shape (frontmatter parsing, unique names, sorted index) as the
prompt loader in 1.5. **Do not** ship a `load_skill` tool in P0: it is a second text
channel into context and one more round on a money path; five prompt files cover P0.

### 1.9 Also adopted, briefly

- `ToolOutcome` (`ref: streaming.py`): tool results are dicts with `ok`, and when held,
  `blocked: <gate>` plus `reason_key`; the model always receives a result, never an
  exception (`ours: capabilities/broker.py::make_tool_error_gate` already does this).
- `AgentEvent` shapes (`ref: streaming.py`) as `core/events.py`; SSE framing in the API.
- Merchant staging (`ref: merchant_agent/changes.py`): guardrails at stage and again at
  apply, one line per target and field. Ours has no apply (section 2.3), so guardrails
  run at stage and the proposal record carries them for the human who applies.

## 2. Reject, and why

### 2.1 Model-routed delegation (`ref: delegation.py`, sub-agent manifests)

The reference's delegate is a model call behind a tool with a brief and a schema. Its
Managed Agents path lets the platform route. Ours routes in `harness/router.py` by
structured intent: what the buyer is looking at (session has a checkout in
PENDING_APPROVAL; an order id in the message), plus a cheap lexicon classifier, plus an
explicit typed `HandBack` a specialist returns when the request is not its job. It is
unit-testable for certainty and it costs no model call. The honest cost: a lexicon
misroutes more often than a model would, and a misroute costs one hand-back turn. We
accept that because a misroute changes no state.

### 2.2 `tool_choice` forcing as the *only* enforcement (their Messages API path)

ADK can force a function (`FunctionCallingConfig(mode=ANY, allowed_function_names=)`)
but the model still writes the arguments. For rules whose input the harness already
knows (a sku token, the session's checkout id, an order id) we **prefetch**: the harness
runs the read itself through the same gated tool, and puts the fenced result in the user
content. Forcing is used only where the input is the model's to write (a terms query).

### 2.3 Merchant `apply_change` behind a host mark (`ref: merchant_agent/gates.py`)

The reference ships `apply_change` on the agent surface and gates it on
`require_host_approval`, a config flag that can be turned off. Spec 6.5: money approval
is never a merchant-configurable switch. Our Registry A has `merchant.growth_proposal.create`
and no apply verb; applying is a human on the merchant console (Registry D, empty in P0).
Nothing to gate because nothing exists.

### 2.4 Session-scoped money facts

The reference enriches cards from session records that may be many turns old. Prices move
under a checkout (that is the demonstration). Our money ledger stays per turn: an amount
in prose must come from a tool result *this turn*. Provenance of ids is per session (1.3);
money facts are per turn.

### 2.5 Memory tools (`save_memory`, `recall_memories`, extraction)

Out of P0: spec 6.2 requires consent, purpose and TTL for preferences, and no
demonstration step needs them. Deferred.

### 2.6 Where our way is worse, honestly

- Our fence today is weaker than theirs on hidden Unicode and marker reassembly (1.1).
- We had no grounding before the model at all (1.2); the reference has had it since day one.
- `ours: capabilities/registry.py` uses invented capability strings (`catalog.read`,
  `basket.write`) that do not match spec 5.3 Registry A, and an `AgentRole` set
  (coordinator/discovery/checkout) that does not match the roster. Both are replaced.
- The factory (`ours: capabilities/tools.py`) returns bare `FunctionTool`s and the gate is
  attached elsewhere, so the roster's "the factory applies the gate" is not yet true.
  The factory now returns a `BoundToolset` (tools plus callbacks) and a test proves every
  `LlmAgent` uses exactly it.
- Their turn loop settles interrupted tool calls so the stored conversation stays
  consistent; ours never thought about it. ADK's session service owns history, but the
  timeout path (1.6) must still append a synthetic error event or drop the partial turn.

## 3. Mapping table: theirs -> ours -> what changes on Gemini/ADK

| Reference module | Our module | Translation |
| --- | --- | --- |
| `commerce_common/fencing.py` | `grounding/fence.py` | Same sanitizer. Two `Fence` literals. Plus our `scan`/withhold. |
| `commerce_common/grounding.py` | `grounding/rules.py` | Same primitives. Enforcement: prefetch by harness, or `before_model_callback` sets `llm_request.config.tool_config = ToolConfig(FunctionCallingConfig(mode=ANY, allowed_function_names=[tool]))` on round 1 only, and clears it after, or the model loops. |
| `shopping_agent/gates.py`, `types.py::remember` | `core/gates.py`, `core/provenance.py` | Gates are pure; called inside the tool closure. Session lock keyed by ADK `session.id`. Provenance persisted via `tool_context.state[...]` so it survives turns. |
| `merchant_agent/gates.py`, `changes.py` | `core/staging.py`, `specialists/growth/` | Guardrails at stage only; no apply tool exists. Proposal ids enter provenance. |
| `commerce_common/turn.py` | `core/turn.py`, `runtime_adk/turn_runner.py` | ADK `Runner` owns the loop. Round cap via `before_model_callback`; last round `FunctionCallingConfigMode.NONE`. `RunConfig(streaming_mode=NONE)`, never `run_live`. One INFO line per model call in `after_model_callback` with `session_tag`, usage from `llm_response.usage_metadata`. |
| `commerce_common/prompt_assembly.py`, role `prompt.py` | `core/prompt.py`, `prompts/*.md` | Static -> `LlmAgent.instruction`; dynamic fenced block -> appended to user `Content`. No cache markers. Hour-rounded clock kept. |
| `commerce_common/presentation.py` | `rendering/cards.py` | Pydantic payloads; `present_*` tools take ids only; `ui` event to the host. |
| `commerce_common/streaming.py` | `core/events.py` | Same event names; SSE framing in `commerce-api`. |
| `commerce_common/skills.py` | `core/prompt.py::load_prompt` | Loader only; no `load_skill` tool. |
| `commerce_common/delegation.py` | `harness/router.py` | Rejected; deterministic router with typed `HandBack`. |
| `shopping_agent/backend.py` | `backends/base.py` | Ours kept; stricter. |
| `runtime-agent-sdk/agent.py` | `runtime_adk/adapter.py` | `make_options` -> `make_agent(spec, binding) -> LlmAgent`. `allowed_tools` has no analogue: the tool list **is** the allow-list. `permission_mode="dontAsk"` -> `FunctionTool(require_confirmation=False)` always. `max_turns` -> round cap. `hooks` -> `after_tool_callback`. |
| SDK `close_on_presentation_hook` | `after_tool_callback` | A `present_suggestions` success marks the turn closable; the next `before_model_callback` sets mode NONE. |

**The denial rule.** ADK runs `before_tool_callback`; a returned dict *is* the tool
result and the tool does not run; `None` runs the tool; `{}` is falsy and ADK treats it
as `None`. Every denial and every error-gate result is therefore a non-empty dict with
`denied: True`, `reason_key`, `tool`, `principal_id` and an `instruction` line
(`ours: capabilities/broker.py` already does this). The gate is registered as one
callable, never a list, so a later callback cannot reset it.

**Callback ownership.** `before_tool_callback` = capability gate (broker).
`on_tool_error_callback` = error gate. `before_model_callback` = round counter, grounding
force, presentation close. `after_model_callback` = model-call log, and reply capture for
the post-check. `after_tool_callback` = provenance recording and presentation close. Only
`runtime_adk/callbacks.py` registers any of these.

## 4. Gates enforced in code

Each gate names the test that proves it. All test basenames are `test_ar_*` and unique
repo-wide. "Exists" means the code exists today and only moves.

| # | Gate | Where | Test |
| --- | --- | --- | --- |
| 1 | Absent verbs: no approve/pay/refund/revoke/cancel/capture on `CommerceBackend`; no tool maps to Registry B, C or D | `backends/base.py`, `capabilities/registry.py` | `test_ar_registry_disjoint.py` (exists in part) |
| 2 | Capability gate: `before_tool_callback` denies with a non-empty dict; the tool never runs | `capabilities/broker.py` | `test_ar_capability_gate.py::test_denial_is_nonempty_dict_and_tool_does_not_run` |
| 3 | Subset at binding: a specialist principal is `harness ∩ allowlist`; widening raises | `harness/binding.py`, `broker.derive_principal` | `test_ar_binding.py::test_specialist_cannot_exceed_harness` |
| 4 | Factory-only tools: every `LlmAgent` in `specialists/` carries exactly the factory's tools and the factory's gate | `capabilities/tools.py`, `runtime_adk/adapter.py` | `test_ar_factory_only.py` (walks every spec, compares object identity) |
| 5 | Tool budget per turn, counted before the tool runs | `core/turn.py` | `test_ar_capability_gate.py::test_budget_exhausted_denies` |
| 6 | Model round cap; last round runs with function calling NONE | `runtime_adk/callbacks.py` | `test_ar_turn_runner.py::test_round_cap_forces_text_round` |
| 7 | Turn timeout leaves state unchanged and renders the fallback | `runtime_adk/turn_runner.py` | `test_ar_turn_runner.py::test_timeout_leaves_backend_untouched` |
| 8 | Provenance: a basket write, a checkout submit, a present call, a resolution evaluate, a proposal create accept only ids a tool returned this session | `core/gates.py` | `test_ar_provenance.py` (one case per write tool, plus the 200-cap eviction) |
| 9 | Quantity cap on the line after the write; basket line-count cap | `core/gates.py` | `test_ar_quantity_caps.py` |
| 10 | Per-session write serialization: two concurrent `basket_set_line` calls in one round do not interleave | `core/gates.py` | `test_ar_write_serialization.py` (asyncio.gather, ordered backend spy) |
| 11 | Fence: label reproduction, nested markers, bidi and tag characters, forged turn markers all neutralized; fixpoint | `grounding/fence.py` | `test_ar_fence.py` (parametrized corpus, including the reference's `</label</label>>`) |
| 12 | Fence notice enters the instruction; merchant text never does | `core/prompt.py` | `test_ar_prompt.py::test_dynamic_block_is_fenced_and_not_in_instruction` |
| 13 | Grounding rules: precedence, whole-word, id token, unseen-only; prefetch result is fenced | `grounding/rules.py`, `harness/grounding.py` | `test_ar_grounding_rules.py` (pins the same five properties as `ref: test_grounding.py`) |
| 14 | Forced first tool is set on round 1 only and cleared after | `runtime_adk/callbacks.py` | `test_ar_turn_runner.py::test_forced_tool_config_is_cleared_after_round_one` |
| 15 | Reply post-check drops sentences with ungrounded SKUs, amounts or success claims | `grounding/postcheck.py` (exists) | `test_ar_postcheck.py` |
| 16 | Money facts per turn; a price from last turn is not evidence | `grounding/ledger.py` | `test_ar_postcheck.py::test_stale_turn_amount_is_ungrounded` |
| 17 | Every recovery code and every delta renders from templates; admission is never worded as payment | `rendering/messages.py` (exists) | `test_ar_messages.py` (exists) |
| 18 | Identity is never a tool parameter: no tool schema has `tenant`, `session`, `principal`, `user` fields | `capabilities/tools.py` | `test_ar_tool_schemas.py::test_no_identity_parameters` |
| 19 | Text mode only: `RunConfig` never streams audio, `run_live` is never referenced, `require_confirmation` is always False | `runtime_adk/` | `test_ar_adapter_config.py` (source grep plus object check) |
| 20 | No specialist prompt file is created or edited by this package; loader falls back when a file is missing | `core/prompt.py` | `test_ar_prompt.py::test_fallback_when_file_absent` |
| 21 | Language and locale come from the harness, never from the model | `language.py` (exists), `harness/session.py` | `test_ar_language.py` |
| 22 | Hand-back is typed; the router never reads model prose to route | `harness/router.py` | `test_ar_router.py` |
| 23 | Growth proposals stage only: guardrails at stage, no apply path exists | `core/staging.py` | `test_ar_staging.py::test_no_apply_tool_in_registry_a` |
| 24 | Support never names an amount outside a plan or provider record (post-check over the plan ledger) | `specialists/support/`, `grounding/ledger.py` | `test_ar_support_amounts.py` |
| 25 | No core module imports `google.adk` or `google.genai` | `runtime_adk/` only | `test_ar_import_boundary.py` |

Rows 6-11, 13 and 14 are enforced here and prompt-side (or absent) in the reference's
safety table. What is still asked of the model shrinks to: fenced text is material,
confirm a write after its result, name products by id. Money is rows 15-17 and 24.

## 5. File layout

Four units, no shared files. `__init__.py` exports listed here
are the contract; a unit may add private modules under its own directory.

```
packages/agent-runtime/src/agent_runtime/
  language.py                   keep
  prompts/                      {shopping,checkout,support,growth,case}_specialist.md

  core/                         UNIT A  (runtime-agnostic; imports no google.*)
    __init__.py
    turn.py                     TurnContext (moved from ../turn.py) + rounds, started, session_tag
    provenance.py               SessionProvenance, remember(), PROVENANCE_CAP, state key
    gates.py                    check_sku_provenance, check_checkout_provenance, check_order_provenance,
                                check_proposal_provenance, check_quantity, check_line_count,
                                session_write_lock, Held outcome
    staging.py                  ProposalDraft, check_guardrails, ProposalLedger (stage only)
    events.py                   AgentEvent, ToolOutcome-style result helpers (ok/held/error dicts)
    prompt.py                   load_prompt(name) with fallback, PromptSpec, dynamic_context()
    specs.py                    SpecialistSpec(name, role, tools, rules, prompt_name, cards)

  grounding/                    UNIT A
    fence.py                    Fence, MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE, sanitize_label, scan
    rules.py                    GroundingRule, matches_any, matches_terms_and_cues, find_token, first_rule
    ledger.py                   keep (per turn) + PlanLedger for support
    payloads.py                 keep; switch to Fence.fence_payload
    postcheck.py                keep

  capabilities/                 UNIT B
    registry.py                 Capability = spec 5.3 Registry A strings; AgentRole = shopping|checkout|
                                support|growth|case; REGISTRY_A tool->capability; AGENT_ALLOWLIST
    broker.py                   derive_principal, make_capability_gate, make_tool_error_gate (keep)
    tools.py                    build_toolset(spec, backend, session, turn) -> BoundToolset(tools, gate,
                                error_gate); every tool closure calls core.gates first
    presenting.py               present_* tools built from rendering.cards, ids only

  specialists/                  UNIT B
    shopping/{__init__,spec,rules}.py      tools: search, product, basket_create, basket_set_line,
                                           basket_get, present_products, present_basket
    checkout/{__init__,spec,rules}.py      tools: basket_get, checkout_create, checkout_get,
                                           checkout_submit_approved, order_track, present_approval,
                                           present_decision
    support/{__init__,spec,rules}.py       tools: order_track, checkout_get, policy_search,
                                           resolution_evaluate, support_escalate, support_case_read,
                                           present_plan
    growth/{__init__,spec,rules}.py        tools: catalogue_health_read, inventory_anomalies_read,
                                           checkout_metrics_read, growth_proposal_create, present_metrics
    case/{__init__,spec,rules}.py          tools: support_case_read, present_case (read-only)

  backends/                     UNIT B (additions only)
    base.py                     add policy_search, resolution_evaluate, support_escalate,
                                support_case_read, merchant reads, growth_proposal_create
    memory.py, http.py          implement the additions

  runtime_adk/                  UNIT C  (the only package importing google.adk / google.genai)
    __init__.py
    adapter.py                  make_agent(spec, binding) -> LlmAgent; instruction from core.prompt
    tools.py                    FunctionTool per ToolSpec; require_confirmation=False
    callbacks.py                before_model (rounds, force, close), after_model (log, capture),
                                after_tool (provenance, close); wires broker gates
    turn_runner.py              run_turn(agent, session, message) -> TurnResult under asyncio.timeout
    session.py                  ADK session service wrapper; provenance state key

  harness/                      UNIT C
    __init__.py
    session.py                  CopilotSession: tenant, buyer/merchant id, locale, modality, correlation
    binding.py                  bind(principal, spec) -> Binding; subset enforced here
    router.py                   route(session, message) -> SpecialistSpec; HandBack handling
    grounding.py                prefetch(rules, message, toolset) -> fenced preamble
    razorai.py            RazorAI.turn(message) -> TurnResult (no model, no prompt)
    merchant_copilot.py         MerchantCopilot.turn(message)

  rendering/                    UNIT D
    messages.py, money.py       keep
    cards.py                    card payload models + builders from ledger facts
```

Tests: `tests/test_ar_<gate>.py` per section 4, owned by the unit that owns the gate's
module; Unit D also owns `test_ar_factory_only.py`, `test_ar_import_boundary.py`,
`test_ar_adapter_config.py` and the end-to-end `test_ar_eleven_steps.py` over
`InMemoryBackend` with a scripted model stub. No test calls Vertex.

Build order if short of time: A and B (fence, rules, gates, registry, factory, shopping
and checkout specs), then C to make them reachable, then D's cards and end-to-end.
