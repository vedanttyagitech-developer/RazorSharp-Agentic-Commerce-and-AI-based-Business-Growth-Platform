export type CopilotExecution = {
  specialist: string | null;
  runtime: string | null;
  routingReason: string | null;
  serverAuthored: boolean | null;
  fallback: boolean | null;
  tools: string[];
  corrections: string[];
};
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
const text = (value: unknown): string | null =>
  typeof value === 'string' ? value : null;
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((x): x is string => typeof x === 'string')
    : [];
export function executionFromResponse(value: unknown): CopilotExecution {
  const body = record(value),
    structured = record(body.structured),
    bridge = record(structured.bridge);
  return {
    specialist: text(bridge.specialist) ?? text(body.specialist),
    runtime: text(bridge.runtime),
    routingReason: text(body.routing_reason),
    serverAuthored:
      typeof body.server_authored === 'boolean' ? body.server_authored : null,
    fallback:
      typeof structured.fallback === 'boolean'
        ? structured.fallback
        : structured.fallback === 'deterministic_runner'
          ? true
          : null,
    tools: Array.isArray(body.tool_calls)
      ? body.tool_calls
          .map((x) => text(record(x).name))
          .filter((x): x is string => x !== null)
      : [],
    corrections: strings(bridge.corrections),
  };
}
export function savedExecution(value: unknown): CopilotExecution | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const x = record(value);
  return {
    specialist: text(x.specialist),
    runtime: text(x.runtime),
    routingReason: text(x.routingReason),
    serverAuthored:
      typeof x.serverAuthored === 'boolean' ? x.serverAuthored : null,
    fallback: typeof x.fallback === 'boolean' ? x.fallback : null,
    tools: strings(x.tools),
    corrections: strings(x.corrections),
  };
}
