# Narration — 5:00

Timed to the composition in `src/Pitch.tsx`. **746 spoken words**, which is 298 seconds at
150 words per minute — so it fits 5:00 at a normal, unhurried read, with no room to dawdle.
At a slower 140 wpm it runs about twenty seconds long; the five beats marked `tight` below
are the ones to compress if that happens, and none of them carries the argument.

The pace matters because this pitch asks a judge to follow a *refusal* and a *proof chain*,
and neither survives being rushed. If you have to choose, protect 2:03 and 3:34.

**The narration never reads the slide.** Every frame already carries its own words. The
voice-over says the thing the frame cannot: why it matters, what it cost to build, what the
alternative would have been. If you find yourself reading the screen aloud, cut the line.

Timestamps are the start of each beat. `[N]` after a heading is the target word count.

---

## 0:00 · The claim

**0:00 — Hook [40]**

> Every agentic-commerce demo shows the same thing. An assistant finds a product and buys it.
> That part is solved. Nobody shows you what happens when the assistant is wrong, or when the
> shop moves underneath it. That is the whole problem.

**0:18 — Problem [33]**

> Three failures cost payment companies real money. A buyer charged twice. An outcome nobody
> can determine. A charge the buyer never agreed to. None is a failure of intelligence. All
> three are failures of authority.

## 0:32 · The idea

**0:32 — Three parties [38]** `tight`

> So the architecture starts from authority, not from the model. An agent proposes. A person
> approves. A kernel authorizes. The model can search the catalogue and draft a bill. It
> cannot approve one, and it cannot reach a payment provider at all.

**0:48 — One function [45]**

> Every rupee leaves through one function, in one transaction, trusting nothing the caller
> passed it. Version, approval, live prices — all re-read under lock. And when it says no, it
> says no with an HTTP 200. A refusal is an answer, not a crash.

## 1:07 · Capability absence

**1:07 — No row [38]**

> This is the part to check first. The model cannot pay — not because something stops it,
> because the capability has no row. Approve, execute, capture: those names do not exist in
> the table the specialists are built from.

**1:23 — By identity [33]** `tight`

> And the gate does not compare names — a name can be forged. It compares the function object
> itself. Build a fake tool, name it exactly right, attach it: still refused. That is why
> prompt injection is not a risk category here.

## 1:37 · A live refusal

**1:37 — Setup [28]**

> Now watch it work. A buyer approved this bill. Then the merchant raised one item by twelve
> rupees and dropped the delivery fee by twelve rupees. The total did not change.

**1:49 — Three comparisons [33]**

> A system that compares totals sees two identical numbers and pays. This one runs three
> comparisons. The first two find nothing — correctly. The third compares the *documents*, and
> finds three fields that moved.

**2:03 — The refusal card [38]**

> The payment is refused, and this is what the buyer is handed. Why. Whose action caused it —
> the merchant's, not theirs. Whether money moved: no attempt row, no grant. And what happens
> next: version three retired, version four waiting.

**2:19 — Timing [42]**

> They re-approved, and it went through. So where did the seconds go? Forty-one of a human
> deciding. Under half a second of kernel. Twenty-two at Razorpay — and that last bar is the
> only span a payment provider can see.

## 2:37 · Exactly once

**2:37 — Commit before send [35]** `tight`

> Duplicate charges come from one ordering mistake: calling the provider before the record is
> durable. Here the grant is spent and committed first. A worker that dies mid-flight replays
> into an already-consumed grant — a resume path, not a failure.

**2:52 — One winner [35]**

> And only one attempt can be live at a time. Not an application check — a partial unique
> index. Proven with twelve real threads on twelve real sessions: one winner, eleven losers,
> one attempt row, one grant.

**3:07 — Unknown ≠ failed [35]**

> Then the expensive one. When a provider call times out, most systems call it a failure. If
> it actually landed, that buyer pays twice. Here, exactly five statuses mean "definitely did
> not happen". Everything else becomes reconciliation.

## 3:22 · Proof

**3:22 — The ask [28]** `tight`

> All of that is a claim about how the system behaves. So here is the endpoint that makes it
> checkable by someone who did not write it and does not trust us.

**3:34 — The chain [58]**

> One GET returns the whole chain of a sale: intent, merchant state, checkout, the frozen
> policy receipt, the approval, the kernel's decision, the grant, the command, the provider
> requests, the evidence, the final state. Ten links, each naming the one before it by hash,
> and fifteen named checks against them. Not a stored certificate — rebuilt and re-verified on
> every request.

**3:59 — Verdict [42]**

> On a real order in this repository all fifteen pass, against a real Razorpay order on
> test-mode APIs, with the audit streams verified in the same answer. Which means a merchant
> can settle a dispute without us, and an auditor without our cooperation.

## 4:17 · Breadth

**4:17 — Numbers [30]** `tight`

> Briefly, on scale. Nearly six thousand tests. But two numbers matter more than that total:
> seventeen hundred hit a real PostgreSQL, and six hundred and forty are written to attack the
> thing they cover.

**4:30 — Surfaces [28]**

> Buyer, voice, copilot, delegated spend, merchant, evidence, operations, and both agent
> protocols — MCP and ACP. Nine surfaces, eighty-seven routes. Every one of them proposes. Not
> one of them can authorize.

## 4:42 · Close

**4:42 — Close [42]**

> Anyone can build an agent that spends money. The question is who is allowed to, and who can
> prove it afterwards. An agent proposes. A person approves. A kernel authorizes. And every
> sale can be re-verified by someone who trusts none of us.

---

## Recording notes

- **Slow down at 2:03 and 3:34.** Those two beats carry the argument. Everything else can be
  read at pace; those two need air.
- **Do not smile through the problem beat.** The three failures are expensive and the delivery
  should sound like someone who has paid for one.
- **The numbers beat is the one to cut** if the read comes in long. It is deliberately last
  and deliberately least load-bearing — that is why it sits at 4:17 and not at 0:30.
- Record dry, no music bed under the refusal beat. If music is used at all, drop it out at
  1:37 and bring it back at 4:42.
