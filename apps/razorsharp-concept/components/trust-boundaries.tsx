import {ArrowRight, ShieldCheck, Fingerprint, Workflow, FileCheck2, LockKeyhole} from 'lucide-react';

const protocols = [
  ['ACP', '2026-04-17', 'Compatible interface', 'Checkout interoperability', 'Does not mean live distribution in ChatGPT Instant Checkout.'],
  ['UCP', '2026-08-25', 'Local conformance pin', 'Commerce profile and lifecycle', 'Separate onboarding is required for Gemini or Google AI Mode.'],
  ['MCP', '2025-06-18', 'Compatible interface', 'Scoped tool access', 'Tool access is not buyer consent. Claude is outside the tested runtime.'],
  ['AP2', 'v0.2.0', 'Local conformance pin', 'Cryptographic evidence verification', 'This project narrows the cryptographic profile to ES256. Evidence still passes Kernel checks.'],
];
const stages = [
  ['01', 'Razor AI proposes', 'A purchase request is intent. Model output cannot grant itself financial authority.', Workflow],
  ['02', 'Authority is checked', 'Trusted buyer approval or valid delegated authority must match the exact purchase.', Fingerprint],
  ['03', 'Kernel admits or refuses', 'Locked state, bound content and financial invariants determine the admission decision.', ShieldCheck],
  ['04', 'Executor carries it out', 'A bound Execution Grant and durable command lead to provider execution and evidence.', FileCheck2],
] as const;
export function TrustBoundaries() {
  return <section className="tb-section" id="trust-boundaries" aria-labelledby="tb-title">
    <div className="tb-heading"><span className="tb-eyebrow"><ShieldCheck size={16}/> THE TRUST ARCHITECTURE</span><h2 id="tb-title">Every request has a route.<br/><em>Every authority has a boundary.</em></h2><p>Razor AI helps decide what to ask for. The <strong>Transaction Trust Kernel</strong> decides whether a money action is permitted. A protocol never bypasses that decision.</p></div>
    <div className="tb-flow">{stages.map(([number,title,description,Icon])=><article key={number}><span className="tb-stage"><Icon size={22}/><small>{number}</small></span><h3>{title}</h3><p>{description}</p><ArrowRight className="tb-arrow" size={18}/></article>)}</div>
    <div className="tb-ownership">
      <article className="tb-kernel"><span className="tb-eyebrow"><LockKeyhole size={15}/> FINANCIAL AUTHORITY</span><h3>Transaction Trust Kernel</h3><p>Enforces sale-bound merchant terms <strong>and non-overridable platform financial invariants.</strong></p><ul><li>Approval binds to checkout version and canonical content hash.</li><li>Admission revalidates merchant state, reservations and buyer or delegated authority.</li><li>Payment, refund and Reserve Pay controls preserve refundable balance, grant use and duplicate-execution protection.</li><li>Financial decisions and their audit evidence commit together.</li></ul><div className="tb-rule">Merchant approval cannot weaken these protections.</div></article>
      <article className="tb-controller"><span className="tb-eyebrow"><Workflow size={15}/> MERCHANT OPERATIONS</span><h3>Merchant Action Controller Command</h3><p>Governs who may propose and approve merchant changes, then invokes the business module that owns the operation.</p><ul><li>Merchant identity, permissions, typed proposals and action-content hash.</li><li>Human approval and the merchant-action lifecycle.</li><li>Catalogue, inventory, pricing and Merchant Policy remain business-domain concerns.</li><li>Financial remedies require a separately authorised financial API and Kernel path.</li></ul><div className="tb-rule">Cannot rewrite buyer approvals, checkout versions, Policy-at-Sale Receipts or Execution Grants.</div></article>
    </div>
    <aside className="tb-midflight"><span>WHEN TERMS CHANGE</span><h3>A new offer is not permission to change an approved purchase.</h3><p>The Kernel compares the approved checkout with current merchant state. A material financial change invalidates the old approval and returns a structured delta for fresh review. Existing sale evidence remains bound to its Policy-at-Sale Receipt.</p></aside>
    <div className="tb-protocol-heading"><span className="tb-eyebrow">INTEROPERABILITY ≠ AUTHORITY</span><h3>Protocols carry requests and evidence.<br/>They do not mint permission to spend.</h3><p>These are the repository’s declared pins and claim boundaries, not a live conformance report or an external certification.</p></div>
    <div className="tb-table-wrap"><table><caption>Protocol boundaries declared in commerce_protocols/core/pins.py</caption><thead><tr><th>Protocol / pin</th><th>Role / declared boundary</th><th>Where the claim stops</th></tr></thead><tbody>{protocols.map(([name,version,boundary,role,limit])=><tr key={name}><th scope="row">{name}<code>{version}</code></th><td><strong>{role}</strong><span>{boundary}</span></td><td>{limit}</td></tr>)}</tbody></table></div>
    <div className="tb-proof-grid"><article><span className="tb-eyebrow">RESERVE PAY / UAP</span><h3>Governed spending. Simulated rail.</h3><p>Selected-product scope, capacity, authority epoch and revocation constrain Reserve Pay. The provider path is a labelled simulation, not a live NPCI or bank connection. UAP is not an additional pin in the four-protocol matrix above.</p></article><article><span className="tb-eyebrow">EVIDENCE, NOT A GREEN TICK</span><h3>The proof is recomputed.</h3><p>The Money Action Proof Chain reads committed rows, recomputes the checkout hash and verifies audit chains. Missing evidence stays missing. A homepage diagram is not a successful payment or a verifier result.</p></article></div>
    <details className="tb-sources"><summary>Inspect the implementation grounding <span>6 source references</span></summary><p>This explanation is grounded in repository code. Runtime results depend on the deployed configuration and the individual transaction.</p><dl>{[
      ['Atomic admission and reapproval','packages/transaction-kernel/src/transaction_kernel/admission.py'],
      ['Merchant ownership and import boundary','packages/merchant-controller/src/merchant_controller/__init__.py'],
      ['Protocol pins and disclaimers','packages/commerce-protocols/src/commerce_protocols/core/pins.py'],
      ['Published protocol status','packages/commerce-api/src/commerce_api/routers/protocols.py'],
      ['Reserve Pay API and simulation boundary','packages/commerce-api/src/commerce_api/routers/reserve.py'],
      ['Committed evidence and hash recomputation','packages/commerce-api/src/commerce_api/services/proof_chain.py'],
    ].map(([label,path])=><div key={path}><dt>{label}</dt><dd><code>{path}</code></dd></div>)}</dl></details>
  </section>;
}
