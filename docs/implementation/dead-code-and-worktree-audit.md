# Dead-code and worktree audit — 10 September 2026

Inspected the backend checkout, the independent `apps/razorsharp-concept` Git repository, and all 15 registered backend worktrees. Authorship of uncommitted files cannot be established from Git status alone.

## Current code

- Preserved and committed the existing pre-epoch order-reference fix (`9e8ae6a`). Its 23 focused tests pass. It prevents a parsed 1969 date from becoming a negative UUID timestamp bound.
- Removed four unreachable frontend modules: `support-cases.tsx`, `merchant-decision-brief.tsx`, `merchant-signals.tsx`, and `revenue-motion.tsx`. Static import/re-export/literal dynamic-import traversal from app entry points, followed by source-reference searches, established that they had no active route consumer. The localStorage support/refund simulator is superseded by `live-orders.tsx` and the authenticated merchant helpdesk.
- Updated the homepage support source reference to `live-orders.tsx`.
- Removed unused imports, obsolete state, an unreachable catalogue-edit dialog, its fields, an unused checkout callback, and an unused sample-report exporter. Enabled TypeScript unused-local/parameter checks to prevent these leftovers recurring.
- Retained reachable campaign and operations previews. Being a preview does not make a component dead code. Their production integration remains separate work.
- Retained `durable_work` and `merchant_sim`: these are still imported by current backend code. Old-looking package names are not evidence that a package can be deleted.

## Historical Claude worktrees

Reviewed and checkpointed the following leftovers **on their existing branches**, without merging them into current main:

| Worktree | Checkpoint | Disposition |
|---|---|---|
| competent-nash-b17d00 | ae81cee | Historical fault-enum fix and tests; current renamed API/executor already contain the fix and parity test. |
| gracious-meitner-01f141 | 29ebd44 | Legacy growth-proposal contract test; references retired merchant-console APIs. |
| sad-borg-e04e08 | 6df4910 | Duplicate legacy test plus its golden fixture; preserved together. |
| nifty-borg-f2725f | 4cb1b36 | Old buyer-web dependency pins; not transplanted into the Vinext frontend. |
| sleepy-tu-fdaed1 | 1e5a795 | Historical local launch configuration, containing runtime/port changes only. |

These are recovery checkpoints, not claims that historical branches pass current tests. No worktrees were deleted. Other registered worktrees were clean at inspection.

## Validation and limits

- Backend package Ruff scan: passed.
- Order-reference tests: 23 passed.
- Frontend TypeScript, including unused locals/parameters: passed.
- Focused frontend transport tests: 16 passed.
- Production frontend build: passed; final cleanup is rebuilt before commit.
- Broader frontend lint still reports 98 diagnostics; unused-variable diagnostics are gone. Remaining React, accessibility and framework-rule findings are not waived or claimed fixed.

The parent repository reports `?? apps/` because `apps/razorsharp-concept` is a separate Git repository. Its source is committed independently. This audit does not silently ignore it, create an unconfigured gitlink, flatten its history or publish it. A parent-only clone is not a complete frontend checkout; repository distribution remains an explicit packaging decision.

This is a static dead-code and working-tree audit, not proof that every Python export is unused or that all integrations work. Dynamic registration, public contracts, migrations, tests, reusable UI primitives and existing financial/audit behavior are preserved.
