# Demo entry point

Use the simulator/test profile. Reserve here is a signed local simulator authorization,
not a bank mandate or a live Reserve Pay integration. Razorpay Checkout is a separate
provider path. Never describe an unknown outcome as a confirmed failed payment.

## Start and inspect

Build with `npm run build` in `apps/razorsharp-concept`, then run
`bash scripts/run_mounted_demo.sh` from the repository root. Open
`http://localhost:8000/shop`. The API serves the entire frontend directly.
The API defaults to port 8000. Follow `scripts/run_demo.sh` diagnostics if configuration
or a dependency is missing. For a fresh local environment use `make bootstrap` and
`make seed` first; do not bootstrap over a deployed database.

The worker must be running for payment execution, reconciliation and automatic refunds.
Scenario routes require the configured scenario key and are disabled in production.
Detailed payloads and prerequisites are in [SCENARIO_RUNBOOK.md](SCENARIO_RUNBOOK.md).

## Eleven demonstration steps

1. Show the actual catalogue. Ask to add an existing product and quantity.
2. Correct the quantity or remove a line; show the resulting cart.
3. Review the exact checkout, amount and bound Merchant Policy / Policy-at-Sale Receipt.
4. Approve and submit. Inspect one payment attempt and its single-use Execution Grant.
5. On a fresh checkout, change the price after review. A changed total, including a lower
   one, requires fresh review; the old approval does not authorize the new checkout.
6. Submit the same approved checkout concurrently. Show the winning attempt and the
   duplicate response, not two provider orders.
7. Create one signed simulator Reserve permission. A second live permission for the same
   buyer/merchant returns a conflict. Exhausted/reconciling permissions retain their slot.
8. Make a permitted Reserve purchase. Show allocated capacity and its terminal settlement;
   unknown outcomes keep capacity held and are reconciled.
9. Engage tenant Safe Mode. Show delegated admission/execution blocked while buyer
   protective paths remain available. Global Safe Mode also blocks unswept Reserve grants.
10. Use the supported timeout or late-capture scenario. Show reconciliation or the automatic
    refund command and blocked fulfilment; only claim completion when provider evidence exists.
11. Demonstrate `LLM_FAILURE` or `TTS_FAILURE` with the configured turn/voice runtime.
    Show the fallback text and unchanged financial state where appropriate.

## Evidence and release checks

- Metrics are process-local best-effort counters, published after transaction commit;
  they are not the financial audit ledger. Rollback discards pending metrics, including
  savepoint work. Worker counters belong to the worker process.
- Money-action mode gates require READ COMMITTED isolation; a stale snapshot is refused.
- Lock order for Reserve is mode gates (where execution/admission applies), checkout,
  authority, approval, reservation, payment attempt, Execution Grant. A path can omit
  rows it does not use. Attempt location reads precede locking and do not authorize work.
- Migration `e8b421cc901a` adds the unique live Reserve scope. It locks and checks existing
  data before creating the index. Duplicate scopes stop migration without rewriting
  authority evidence. Inspect and revoke superseded permissions through the audited
  kernel before retrying; never pick an arbitrary winner in SQL.
- Focused tests: `test_committed_metrics.py`, `test_safe_mode.py`,
  `test_capi_reserve.py`, and `test_capi_reserve_proofs.py`.
- Database tests and simulator tests do not establish live bank/provider behavior,
  production load capacity or a successful cloud rollout. Rehearse the actual deployed
  profile before presenting it.
