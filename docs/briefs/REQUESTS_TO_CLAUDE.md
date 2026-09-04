# Requests from Gemini to Claude

Gemini writes here instead of editing a file it does not own. Claude actions these during
integration. Append; never delete another entry.

Format:

```
## <short title>
File(s): <path>
Why: <what you were doing and why the change is needed>
Proposed change: <the exact edit, if you know it>
Status: OPEN
```

---

## Remove external CDN from CSP imgSrc
File(s): apps/buyer-web/src/lib/security/csp.ts
Why: every product image is now served from apps/buyer-web/public/, so the img-src entry
permitting an external image host is dead and should be removed. Tightening it back to
'self' is a real security improvement, not housekeeping.
Proposed change: delete the external CDN entry from `imgSrc`.
Status: OPEN

---

## Commit and Merge Backend Work to main
File(s): packages/commerce-api/**, packages/durable-worker/**, Makefile, scripts/**
Why: Brief 3 Step 0 states: `git merge --ff-only main` to bring in Claude's work (HTTP API & durable worker). Currently, in `acr-worktrees/claude-backend`, these files are uncommitted/untracked on the filesystem and not merged into `main`. Because Gemini operates strictly in `acr-worktrees/gemini-catalogue` under isolated boundaries, `git merge --ff-only main` reports "Already up to date" and does not pull the backend code.
Proposed change: Commit the backend files on `claude/backend` and merge them into `main` so `gemini-catalogue` can fast-forward merge them cleanly.
Status: OPEN
