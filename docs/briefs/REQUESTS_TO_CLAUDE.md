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

## (example, delete when the first real request arrives)
File(s): apps/buyer-web/src/lib/security/csp.ts
Why: the hotlinked competitor images are gone, so the img-src entry that allowed that CDN
is now dead and should be removed.
Proposed change: delete the `https://cdn.zeptonow.com` entry from `imgSrc`.
Status: OPEN
