"use client";

/**
 * One component per structured payload the copilot can return.
 *
 * Every figure on these cards is read out of the payload. None is derived, summed or
 * carried over from an earlier turn, and a figure the platform could not derive is drawn
 * as *not measured* rather than as a zero -- the two are different answers and the
 * merchant is entitled to the difference. That rule is enforced in one place, `Reading`
 * below, so no card can quietly opt out of it.
 *
 * Each card also states its provenance rather than assuming the reply above it did. 6.6
 * requires a recommendation to cite its source, its window, its sample size and whether
 * the data is synthetic, and a card is where a merchant looks when they stop believing a
 * sentence.
 */
import Link from "next/link";

import { Chip, Empty, Panel, TableWrap, Td, Th, toneForState } from "@/components/ui";
import { formatCount } from "@/lib/money";

import {
  anomalyKind,
  anomalyLabel,
  anomalyName,
  anomalyUnits,
  type Anomaly,
  type CatalogueHealth,
  type CheckoutMetrics,
  type Card as PayloadCard,
} from "./payloads";

/**
 * A count the platform derived, or the fact that it did not.
 *
 * The warn tone is deliberate. "Not measured" is not a neutral blank: it means a figure a
 * merchant might act on is missing, and it should read as something worth noticing rather
 * than as an empty cell they scan past.
 */
function Reading({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return <span className="mono text-[var(--warn)]">not measured</span>;
  }
  return <span className="num">{formatCount(value)}</span>;
}

function Stat({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3 py-2">
      <div className="eyebrow">{label}</div>
      <div className="mt-1 text-[17px] leading-tight text-[var(--ink)]">
        <Reading value={value} />
      </div>
    </div>
  );
}

/**
 * The provenance strip every card carries.
 *
 * `synthetic` is drawn as a chip rather than as a footnote because it changes what the
 * numbers mean. This merchant's catalogue is simulated, 6.6 requires a recommendation to
 * say so, and a card that mentioned it quietly at the bottom would be technically
 * compliant and practically silent.
 */
function Provenance({
  source,
  synthetic,
  window,
  sampleSize,
  revision,
}: {
  source: string;
  synthetic: boolean;
  window?: string | null;
  sampleSize?: number | null;
  revision?: number | null;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-t border-[var(--line-soft)] px-3 py-2">
      <Chip tone={synthetic ? "warn" : "muted"}>{synthetic ? "synthetic data" : "live data"}</Chip>
      <Chip tone="muted">source {source}</Chip>
      <Chip tone="muted">window {window ?? "not stated"}</Chip>
      <Chip tone="muted">
        sample {sampleSize === null || sampleSize === undefined ? "not stated" : formatCount(sampleSize)}
      </Chip>
      {revision !== null && revision !== undefined && (
        <Chip tone="muted">catalogue revision {revision}</Chip>
      )}
    </div>
  );
}

function CatalogueHealthCard({ value }: { value: CatalogueHealth }) {
  return (
    <Panel title="Catalogue health" subtitle="merchant.catalogue_health.read" className="mt-2.5">
      <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3">
        <Stat label="products" value={value.products} />
        <Stat label="listed" value={value.listed} />
        <Stat label="delisted" value={value.delisted} />
        <Stat label="out of stock" value={value.out_of_stock} />
        <Stat label="low stock" value={value.low_stock} />
      </div>
      {/*
        Delisted and out of stock are two different decisions and the card says so, because
        the remedies are opposite: one product is coming back this afternoon if somebody
        orders more of it, and the other was taken off sale on purpose.
      */}
      <p className="px-3 pb-3 text-[11.5px] text-[var(--muted)]">
        Delisted is a product taken off sale. Out of stock is a listed product with no units. They
        are counted separately and they call for different actions.
      </p>
      <Provenance
        source={value.source}
        synthetic={value.synthetic}
        window="all-time"
        sampleSize={value.sample_size}
        revision={value.catalogue_revision}
      />
    </Panel>
  );
}

/**
 * What this row's product is called, drawn as text and never as an instruction.
 *
 * The name is read through `anomalyLabel`, which prefers the plain name and falls back to
 * the platform's own safe label. It deliberately never prints `merchant_text`: that field
 * carries the agent runtime's `<merchant_data>` fence, which exists to tell a *model* what
 * it is reading, and printing the wrapper to a merchant would show them markup where a
 * product name belongs.
 */
function MerchantText({ anomaly }: { anomaly: Anomaly }) {
  const label = anomalyLabel(anomaly);
  if (anomaly.quarantined === true) {
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <Chip tone="warn">quarantined text</Chip>
        <span className="text-[var(--muted)]">{label}</span>
      </span>
    );
  }
  if (!label) return <span className="mono text-[var(--faint)]">—</span>;
  return <span className="text-[var(--ink)]">{label}</span>;
}

function InventoryAnomaliesCard({
  value,
}: {
  value: { anomalies: Anomaly[]; source: string; synthetic: boolean; sample_size?: number | null; catalogue_revision?: number | null };
}) {
  return (
    <Panel
      title="Inventory anomalies"
      subtitle="merchant.inventory_anomalies.read"
      className="mt-2.5"
    >
      {value.anomalies.length === 0 ? (
        <Empty>Nothing was flagged. Every listed product has units.</Empty>
      ) : (
        <TableWrap>
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                <Th>SKU</Th>
                <Th>merchant text</Th>
                <Th align="right">units</Th>
                <Th>listed</Th>
                <Th>flagged as</Th>
              </tr>
            </thead>
            <tbody>
              {value.anomalies.map((anomaly) => (
                <tr key={anomaly.sku}>
                  <Td>
                    <span className="mono text-[var(--ink)]">{anomaly.sku}</span>
                  </Td>
                  <Td>
                    <MerchantText anomaly={anomaly} />
                  </Td>
                  <Td align="right">
                    <Reading value={anomalyUnits(anomaly)} />
                  </Td>
                  <Td>
                    {anomaly.is_listed === null || anomaly.is_listed === undefined ? (
                      <Chip tone="warn">unknown</Chip>
                    ) : (
                      <Chip tone={anomaly.is_listed ? "positive" : "muted"}>
                        {anomaly.is_listed ? "listed" : "delisted"}
                      </Chip>
                    )}
                  </Td>
                  <Td>
                    {/* A producer that flagged a row without saying why is a gap worth
                        seeing, so an absent kind reads as unstated rather than as blank. */}
                    {anomalyKind(anomaly) === null ? (
                      <Chip tone="warn">not stated</Chip>
                    ) : (
                      <Chip tone="warn">{anomalyName(anomalyKind(anomaly) ?? "")}</Chip>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {/*
        The names in this table are merchant-authored strings that travelled through the
        agent. Saying so on the card is not decoration: it is why the copilot may report
        the word and may not act on it.
      */}
      <p className="px-3 py-2 text-[11.5px] text-[var(--muted)]">
        The flag is what the platform observed. Whether to restock, relist or drop a product is
        yours to decide, and any change to stock arrives as a proposal you apply.
      </p>
      <Provenance
        source={value.source}
        synthetic={value.synthetic}
        window="all-time"
        sampleSize={value.sample_size}
        revision={value.catalogue_revision}
      />
    </Panel>
  );
}

function CheckoutMetricsCard({ value }: { value: CheckoutMetrics }) {
  const states = Object.entries(value.checkouts_by_state).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Panel
      title="Checkouts and orders"
      subtitle="merchant.checkout_metrics.read"
      className="mt-2.5"
    >
      <div className="grid grid-cols-2 gap-2 p-3">
        <Stat label="checkouts counted" value={value.sample_size} />
        <Stat label="orders" value={value.orders} />
      </div>
      {states.length === 0 ? (
        <Empty>No checkouts were counted in this window.</Empty>
      ) : (
        <TableWrap>
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                <Th>checkout state</Th>
                <Th align="right">count</Th>
              </tr>
            </thead>
            <tbody>
              {states.map(([state, count]) => (
                <tr key={state}>
                  <Td>
                    <Chip tone={toneForState(state)}>{state}</Chip>
                  </Td>
                  <Td align="right">
                    <Reading value={count} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {/*
        This tool counts rows and sums nothing. A revenue figure is derived by the evidence
        endpoint against committed rows, and a conversion rate is a division the platform
        did not perform -- so the card points at the page that does hold those figures
        rather than producing one here, which would be a number with nothing behind it.
      */}
      <p className="px-3 py-2 text-[11.5px] text-[var(--muted)]">
        These are counts from committed rows. No amount is summed here and no rate is computed:
        revenue is derived row by row on the{" "}
        <Link href="/evidence" className="text-[var(--info)] hover:underline">
          evidence page
        </Link>
        .
      </p>
      <Provenance
        source={value.source}
        synthetic={value.synthetic}
        window={value.window}
        sampleSize={value.sample_size}
      />
    </Panel>
  );
}

export function StructuredCard({ card }: { card: PayloadCard }) {
  if (card.kind === "catalogue_health") return <CatalogueHealthCard value={card.value} />;
  if (card.kind === "inventory_anomalies") return <InventoryAnomaliesCard value={card.value} />;
  return <CheckoutMetricsCard value={card.value} />;
}

/**
 * A payload that arrived and was not drawn, named rather than dropped.
 *
 * Two different things land here and they need different sentences. A kind this console
 * has no component for means the platform grew a card and this app has not caught up. A
 * kind it does draw, arriving in a shape that did not parse, means the two halves have
 * drifted apart -- and that one is worth showing the detail for, because it is otherwise
 * invisible: the reply above it still reads perfectly well.
 */
export function UndrawnPayload({
  undrawn,
  malformed,
}: {
  undrawn: string | null;
  malformed: { kind: string; detail: string } | null;
}) {
  if (!undrawn && !malformed) return null;
  return (
    <div className="mt-2.5 rounded-[var(--r-md)] border border-dashed border-[var(--line)] bg-[var(--raised)] p-3">
      <Chip tone="warn">PAYLOAD NOT DRAWN</Chip>
      {undrawn && (
        <p className="mt-1.5 text-[12px] text-[var(--ink)]">
          The copilot returned a payload of kind{" "}
          <span className="mono text-[var(--ink)]">{undrawn}</span>, and this console has no card
          for it. Nothing was invented in its place.
        </p>
      )}
      {malformed && (
        <>
          <p className="mt-1.5 text-[12px] text-[var(--ink)]">
            A payload of kind <span className="mono">{malformed.kind}</span> arrived in a shape
            this console could not read, so no figure from it is shown.
          </p>
          {malformed.detail && (
            <p className="mono mt-1 text-[var(--muted)] break-id">{malformed.detail}</p>
          )}
        </>
      )}
    </div>
  );
}
