"use client";

/**
 * The Agent Studio: configure one of the platform's built-in specialists for this shop.
 *
 * What it is, and what it deliberately is not. Specification 36 puts "merchant-created,
 * cloned or arbitrary custom agents" and the "full Agent Studio authoring/publish/rollback
 * workflow" outside P0, and closes by saying those exclusions must not appear as half-built
 * UI. So this screen has no create-an-agent flow, no publish button and no rollback list --
 * not greyed out, not hidden behind a flag, absent. What it has instead is the thing
 * specification 7.2 does put in P0: use the built-in agents, enable or disable non-money
 * features, and test the configuration. The exclusion is stated on the screen, in the last
 * panel, because a merchant who wonders whether they can build their own agent deserves the
 * answer rather than a missing menu item.
 *
 * The order of the page is an argument. Compose, then the boundary, then try it. A
 * merchant should tick a capability, read what their assistant can and cannot do as a
 * consequence, and only then ask it something -- because the interesting moment is not the
 * answer, it is discovering that the set of possible answers was fixed before they asked.
 *
 * Every capability string on this page came from `GET /v1/agent/capabilities` in this page
 * load. When that read fails the page renders the problem document and composes nothing:
 * a studio drawing a capability list it could not read would be describing a boundary it
 * had invented.
 */
import { useCallback, useState } from "react";
import Link from "next/link";

import { Chip, Loading, Panel, ProblemPanel } from "@/components/ui";
import { BoundaryPanel } from "@/features/studio/boundary-panel";
import { deriveBoundary } from "@/features/studio/boundary";
import { ComposePanel } from "@/features/studio/compose";
import { SandboxPanel } from "@/features/studio/sandbox";
import {
  clearStoredComposition,
  readStoredComposition,
  reconcile,
  roleIn,
  startingComposition,
  writeStoredComposition,
  type Composition,
} from "@/features/studio/composition";
import { roleTitle } from "@/features/studio/vocabulary";
import { api } from "@/lib/api/client";
import { useRead } from "@/lib/useRead";

export default function StudioPage() {
  const caps = useRead((signal) => api.agentCapabilities(signal), []);

  // The draft this browser is holding, read once and lazily.
  //
  // Lazily because `localStorage` does not exist during the server pass and reading it in
  // the render body would throw there; once, because re-reading it on every render would
  // let another tab's write silently replace what this merchant is in the middle of
  // editing. Nothing derived from it reaches the DOM until the capability read resolves,
  // which is client-only, so the server and the first client render agree on an empty
  // screen and there is nothing to mismatch.
  const [stored, setStored] = useState<Composition | null>(() => readStoredComposition());
  const [saved, setSaved] = useState<boolean | null>(null);

  const first = caps.data?.specialists[0] ?? null;

  // Reconciled during render rather than copied into state by an effect. The roster is the
  // server's and the draft is the browser's, and the composition on screen is a function of
  // both -- so it is computed from both, every render, and cannot drift out of step with a
  // re-read the way a state copy would.
  const restored = caps.data !== null && stored !== null ? reconcile(caps.data, stored) : null;
  const composition = restored?.composition ?? (first !== null ? startingComposition(first) : null);
  const dropped = restored?.dropped ?? [];
  const missingRole = restored?.missingRole ?? null;

  const change = useCallback((next: Composition) => {
    setStored(next);
    setSaved(writeStoredComposition(next));
  }, []);

  const startOver = useCallback(() => {
    clearStoredComposition();
    setStored(null);
    setSaved(null);
  }, []);

  const role = caps.data && composition ? roleIn(caps.data, composition.role) : null;
  const boundary = caps.data && composition ? deriveBoundary(caps.data, composition) : null;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">
            Agent Studio
          </h1>
          <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
            GET /v1/agent/capabilities — configure a built-in specialist for your shop, and see
            exactly what it may do.
          </p>
        </div>
        {caps.data && (
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="muted">{caps.data.copilot} copilot</Chip>
            <Chip tone="muted">{caps.data.actor_type.toLowerCase()} session</Chip>
          </div>
        )}
      </header>

      {caps.loading && (
        <Panel title="Compose">
          <Loading label="Reading the capabilities this session may bind" />
        </Panel>
      )}

      {caps.error != null && (
        <ProblemPanel
          error={caps.error}
          what="the capability document this studio composes from"
          onRetry={caps.reload}
        />
      )}

      {caps.data && caps.data.specialists.length === 0 && (
        <Panel title="Compose">
          <p className="px-4 py-6 text-[12.5px] text-[var(--warn)]">
            The platform bound no specialist to this session. There is nothing to configure, and
            this console will not draw a roster it was not given.
          </p>
        </Panel>
      )}

      {/* -------------------------------------------------------- a draft that moved on */}
      {(dropped.length > 0 || missingRole !== null) && (
        <div
          role="status"
          className="rounded-[var(--r-md)] border border-[color-mix(in_srgb,var(--warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--warn)_10%,transparent)] p-3"
        >
          <Chip tone="warn">DRAFT RECONCILED</Chip>
          {missingRole !== null && (
            <p className="mt-1.5 text-[12px] text-[var(--ink)]">
              Your saved draft was for{" "}
              <span className="mono break-id">{missingRole}</span>, which this session&rsquo;s
              roster no longer offers. It has been started again from the first role the platform
              does offer.
            </p>
          )}
          {dropped.length > 0 && (
            <p className="mt-1.5 text-[12px] text-[var(--ink)]">
              The platform no longer declares{" "}
              <span className="mono break-id">{dropped.join(", ")}</span> for this role, so it has
              been removed from your draft. It is named here rather than dropped quietly: an
              assistant that lost a capability without saying so is one you would go on believing
              it held.
            </p>
          )}
        </div>
      )}

      {caps.data && composition && role && (
        <>
          <ComposePanel
            caps={caps.data}
            role={role}
            composition={composition}
            onChange={change}
            saved={saved}
          />

          {boundary && <BoundaryPanel boundary={boundary} name={composition.name} />}

          <SandboxPanel composition={composition} />

          {/* ------------------------------------------------ what the platform does not do */}
          <Panel
            title="What this studio does not do"
            subtitle="Specification 36 — explicitly outside P0"
            actions={<button type="button" onClick={startOver} className="text-[12px] text-[var(--info)] hover:underline">Start this draft again</button>}
          >
            <div className="space-y-2.5 p-4">
              <p className="max-w-[80ch] text-[12.5px] leading-[1.55] text-[var(--ink)]">
                You cannot create an agent here, clone one, or give one a tool the platform did
                not build. The five specialists are the roster; a merchant configures them and
                does not author them. That is a decision in the specification rather than an
                unfinished screen, and it is the reason the boundary above can be trusted: a
                platform where a merchant could add a tool would be a platform where this page
                could not tell you what your assistant may do.
              </p>
              <p className="max-w-[80ch] text-[12.5px] leading-[1.55] text-[var(--muted)]">
                There is also no publish and no rollback. Specification 7.1 step 16 ends
                onboarding at an immutable merchant-configuration version, and section 36 puts
                that whole workflow outside P0 — so this draft is held in this browser and
                nowhere else. It is not on the platform, it is not on your other devices, and
                clearing your site data ends it.
              </p>
              <p className="mono text-[var(--faint)] break-id">
                {roleTitle(composition.role)} · {composition.capabilities.length} of{" "}
                {role.capabilities.length} capabilities enabled · draft in this browser
              </p>
              <p className="text-[11.5px] text-[var(--muted)]">
                Coming here for the first time?{" "}
                <Link href="/onboarding" className="text-[var(--info)] hover:underline">
                  The onboarding checklist
                </Link>{" "}
                says which of the sixteen steps this shop has actually completed.
              </p>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}
