/**
 * The console's primitives.
 *
 * Two of these carry the honesty rules the whole app is built on, and they are the reason
 * this file exists rather than a scattering of ad-hoc divs:
 *
 *  - `ProblemPanel` renders a failed read as the problem document it was, with its status,
 *    its detail and every extension member the API attached. A page that cannot read its
 *    figures shows this instead of the figures. There is no third option and no cached
 *    last-good value anywhere in this app.
 *  - `NotWired` names an endpoint that does not exist yet. A panel with nothing behind it
 *    says so and names what is missing, rather than filling itself with a plausible
 *    number an operator would act on.
 */
import type { ReactNode } from "react";
import { extensionsOf, problemOf } from "@/lib/api/problem";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

// --------------------------------------------------------------------------- surfaces

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cx(
        "rounded-[var(--r-lg)] border border-[var(--line)] bg-[var(--surface)] overflow-hidden",
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-[var(--line)] px-4 py-3">
          <div className="min-w-0">
            {title && <h2 className="text-[13px] font-semibold text-[var(--ink)]">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-[11.5px] text-[var(--muted)]">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

/** A labelled figure. `hint` says where the number came from; it is never decorative. */
export function Figure({
  label,
  value,
  hint,
  tone = "ink",
  href,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  href?: string;
}) {
  const body = (
    <>
      <div className="eyebrow">{label}</div>
      <div className={cx("num mt-1.5 text-[19px] leading-tight", TONE_TEXT[tone])}>{value}</div>
      {hint && <div className="mt-1 text-[11px] text-[var(--faint)] break-id">{hint}</div>}
    </>
  );
  const className =
    "block rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3.5 py-3 min-w-0";
  if (!href) return <div className={className}>{body}</div>;
  return (
    <a href={href} className={cx(className, "transition-colors hover:border-[var(--info)]")}>
      {body}
    </a>
  );
}

// ------------------------------------------------------------------------------ tone

export type Tone = "ink" | "muted" | "positive" | "warn" | "danger" | "info";

const TONE_TEXT: Record<Tone, string> = {
  ink: "text-[var(--ink)]",
  muted: "text-[var(--muted)]",
  positive: "text-[var(--positive)]",
  warn: "text-[var(--warn)]",
  danger: "text-[var(--danger)]",
  info: "text-[var(--info)]",
};

const TONE_CHIP: Record<Tone, string> = {
  ink: "border-[var(--line)] bg-[var(--raised)] text-[var(--ink)]",
  muted: "border-[var(--line)] bg-[var(--raised)] text-[var(--muted)]",
  positive: "border-[color-mix(in_srgb,var(--positive)_45%,transparent)] bg-[color-mix(in_srgb,var(--positive)_12%,transparent)] text-[var(--positive)]",
  warn: "border-[color-mix(in_srgb,var(--warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] text-[var(--warn)]",
  danger: "border-[color-mix(in_srgb,var(--danger)_45%,transparent)] bg-[color-mix(in_srgb,var(--danger)_12%,transparent)] text-[var(--danger)]",
  info: "border-[color-mix(in_srgb,var(--info)_45%,transparent)] bg-[color-mix(in_srgb,var(--info)_12%,transparent)] text-[var(--info)]",
};

/**
 * A state chip. Always renders its own text, so the state survives a colour-blind reader,
 * a greyscale projector and a screenshot in a slide deck.
 */
export function Chip({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={cx(
        "mono inline-flex items-center gap-1 whitespace-nowrap rounded-[var(--r-sm)] border px-1.5 py-0.5 font-medium",
        TONE_CHIP[tone],
      )}
    >
      {children}
    </span>
  );
}

/**
 * How each state the console renders is coloured.
 *
 * The three refund states this platform is careful about are three different colours on
 * purpose: REFUND_PENDING is in flight (the provider was asked and has not answered),
 * REFUND_UNKNOWN is owned by reconciliation (the answer was lost), REFUND_FAILED is a
 * refusal from the provider. An operator who retries a pending refund because it looked
 * like a failed one refunds a buyer twice.
 */
export function toneForState(state: string): Tone {
  switch (state) {
    case "CAPTURED":
    case "CONFIRMED":
    case "REFUNDED":
    case "DONE":
    case "PROCESSED":
    case "OK":
    case "CONSUMED":
      return "positive";
    case "REFUND_PENDING":
    case "AUTO_REFUND_PENDING":
    case "PENDING":
    case "LEASED":
    case "SUBMITTED":
    case "AUTHORIZED":
    case "CREATED":
    case "PARTIALLY_REFUNDED":
      return "info";
    case "REFUND_UNKNOWN":
    case "UNKNOWN":
    case "RECONCILING":
    case "STALE_CAPTURE":
    case "FULFILMENT_BLOCKED":
    case "ESCALATED":
      return "warn";
    case "REFUND_FAILED":
    case "FAILED":
    case "DEAD":
    case "EXPIRED":
    case "CANCELLED":
      return "danger";
    default:
      return "muted";
  }
}

// ---------------------------------------------------------------------------- buttons

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
  title,
  ariaLabel,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  ariaLabel?: string;
}) {
  const styles: Record<string, string> = {
    default: "border-[var(--line)] bg-[var(--raised)] text-[var(--ink)] hover:border-[var(--faint)]",
    primary:
      "border-[color-mix(in_srgb,var(--info)_60%,transparent)] bg-[color-mix(in_srgb,var(--info)_18%,transparent)] text-[var(--info)] hover:bg-[color-mix(in_srgb,var(--info)_26%,transparent)]",
    danger:
      "border-[color-mix(in_srgb,var(--danger)_60%,transparent)] bg-[color-mix(in_srgb,var(--danger)_16%,transparent)] text-[var(--danger)] hover:bg-[color-mix(in_srgb,var(--danger)_24%,transparent)]",
    ghost: "border-transparent bg-transparent text-[var(--muted)] hover:text-[var(--ink)]",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-[var(--r-sm)] border px-2.5 py-1.5 text-[12px] font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-45",
        styles[variant],
      )}
    >
      {children}
    </button>
  );
}

// --------------------------------------------------------------------------- states

export function Spinner({ label = "Reading" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-[12px] text-[var(--muted)]">
      <svg width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
        <circle cx="6.5" cy="6.5" r="5" fill="none" stroke="var(--line)" strokeWidth="2" />
        <path
          d="M6.5 1.5a5 5 0 0 1 5 5"
          fill="none"
          stroke="var(--info)"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <animateTransform
            attributeName="transform"
            type="rotate"
            from="0 6.5 6.5"
            to="360 6.5 6.5"
            dur="0.8s"
            repeatCount="indefinite"
          />
        </path>
      </svg>
      {label}
    </span>
  );
}

export function Loading({ label = "Reading the platform" }: { label?: string }) {
  return (
    <div className="px-4 py-8 text-center" role="status" aria-live="polite">
      <Spinner label={label} />
    </div>
  );
}

/**
 * A failed read, rendered as the problem document it was.
 *
 * Status, title, detail and every extension member, because the operator reading this
 * screen is the person who has to decide whether the API is down, the key is wrong or
 * the row genuinely does not exist -- and those three look identical behind a friendly
 * sentence.
 */
export function ProblemPanel({
  error,
  what,
  onRetry,
}: {
  error: unknown;
  what: string;
  onRetry?: () => void;
}) {
  const problem = problemOf(error);
  const extensions = extensionsOf(problem);
  return (
    <div
      role="alert"
      aria-live="assertive"
      className="rounded-[var(--r-md)] border border-[color-mix(in_srgb,var(--danger)_45%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="danger">READ FAILED</Chip>
        <span className="text-[12px] text-[var(--muted)]">{what}</span>
      </div>
      <p className="mt-2 text-[13px] font-semibold text-[var(--ink)]">
        <span className="num mr-2 text-[var(--danger)]">{problem.status}</span>
        {problem.title}
      </p>
      {problem.detail && <p className="mt-1 text-[12px] text-[var(--muted)] break-id">{problem.detail}</p>}
      {extensions.length > 0 && (
        <dl className="mono mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[var(--faint)]">
          {extensions.map(([key, value]) => (
            <div key={key} className="contents">
              <dt>{key}</dt>
              <dd className="text-[var(--muted)] break-id">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="mt-3 text-[11.5px] text-[var(--faint)]">
        Nothing is shown in place of these figures. This console has no fixtures to fall back to.
      </p>
      {onRetry && (
        <div className="mt-3">
          <Button onClick={onRetry}>Read again</Button>
        </div>
      )}
    </div>
  );
}

/**
 * A panel whose endpoint does not exist yet.
 *
 * It names the missing thing rather than filling the space, because a plausible number
 * with nothing behind it is worse than a blank: an operator acts on it.
 */
export function NotWired({ what, missing }: { what: string; missing: string }) {
  return (
    <div className="rounded-[var(--r-md)] border border-dashed border-[var(--line)] bg-[var(--raised)] p-4">
      <Chip tone="warn">NOT WIRED YET</Chip>
      <p className="mt-2 text-[12.5px] text-[var(--ink)]">{what}</p>
      <p className="mono mt-1 text-[var(--muted)] break-id">Missing: {missing}</p>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-4 py-8 text-center text-[12px] text-[var(--muted)]">{children}</div>;
}

// ----------------------------------------------------------------------- structure

/** A dense definition row. The label is fixed-width so a column of them aligns. */
export function Field({
  label,
  children,
  source,
}: {
  label: string;
  children: ReactNode;
  source?: string;
}) {
  return (
    <div className="grid grid-cols-1 gap-x-4 border-b border-[var(--line-soft)] px-4 py-2 last:border-0 sm:grid-cols-[190px_1fr]">
      <dt className="text-[11.5px] text-[var(--muted)]">
        {label}
        {source && <span className="mono mt-0.5 block text-[var(--faint)]">{source}</span>}
      </dt>
      <dd className="mt-0.5 min-w-0 text-[12.5px] text-[var(--ink)] break-id sm:mt-0">{children}</dd>
    </div>
  );
}

/** A table that scrolls inside itself rather than widening the page. */
export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="overflow-x-auto">{children}</div>;
}

export function Th({ children, align = "left" }: { children: ReactNode; align?: "left" | "right" }) {
  return (
    <th
      scope="col"
      className={cx(
        "eyebrow whitespace-nowrap border-b border-[var(--line)] px-3 py-2 font-semibold",
        align === "right" ? "text-right" : "text-left",
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  className,
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <td
      className={cx(
        "border-b border-[var(--line-soft)] px-3 py-2 align-top",
        align === "right" ? "text-right" : "text-left",
        className,
      )}
    >
      {children}
    </td>
  );
}

/** An identifier, shortened for the eye and complete in the title attribute. */
export function Id({ value, href }: { value: string; href?: string }) {
  const short = value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
  if (href) {
    return (
      <a href={href} title={value} className="mono text-[var(--info)] hover:underline">
        {short}
      </a>
    );
  }
  return (
    <span title={value} className="mono text-[var(--muted)]">
      {short}
    </span>
  );
}

/** RFC 3339 from the API, rendered in the reader's zone with the exact value on hover. */
export function When({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="mono text-[var(--faint)]">—</span>;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return (
      <span className="mono text-[var(--muted)]" title={value}>
        {value}
      </span>
    );
  }
  return (
    <time dateTime={value} title={value} className="mono text-[var(--muted)] whitespace-nowrap">
      {/* Asia/Kolkata named explicitly: an operator and the buyer they are on the phone
          to must be reading one clock, and the console is as likely to be open on a
          server-rendered page as on the operator's own machine. */}
      {parsed.toLocaleString("en-IN", {
        timeZone: "Asia/Kolkata",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      })}
    </time>
  );
}

/** A boolean the API sent, never inferred. `null` renders as unknown, not as false. */
export function Flag({
  value,
  yes = "yes",
  no = "no",
  yesTone = "positive",
  noTone = "muted",
}: {
  value: boolean | null | undefined;
  yes?: string;
  no?: string;
  yesTone?: Tone;
  noTone?: Tone;
}) {
  if (value === null || value === undefined) return <Chip tone="warn">unknown</Chip>;
  return <Chip tone={value ? yesTone : noTone}>{value ? yes : no}</Chip>;
}
