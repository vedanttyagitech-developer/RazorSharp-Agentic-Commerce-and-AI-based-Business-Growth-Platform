/**
 * The same-origin address of the microphone socket -- which this handler does not serve.
 *
 * `/api/voice/stream` is the path the browser opens in a deployment, and there the request
 * never arrives here: the reverse proxy in front of this app matches the path first and
 * upgrades it to the voice gateway, which is why `connect-src 'self'` is enough and why
 * `defaultVoiceUrl` points at this origin. In local development there is no such proxy, so
 * the client addresses the gateway directly on `ws://127.0.0.1:8100/v1/voice/stream` and
 * `lib/security/csp` names that origin in `connect-src` for development only.
 *
 * WHY THERE IS NO UPGRADE PROXY IN THIS FILE
 * ------------------------------------------
 * Writing one was considered and refused, and the reasoning is recorded in `csp.ts`
 * alongside the policy it shaped. A hand-written WebSocket relay inside a Next route
 * handler would be a second implementation of the transport -- framing, backpressure,
 * close codes, the binary/text split the pipeline depends on -- whose failures would look
 * exactly like the gateway's, and it would exist only in development, which is the worst
 * possible place to keep code that never runs in production. Next's route handlers cannot
 * take over a socket in any case; a handler here can answer a request but cannot become
 * one.
 *
 * So this file is not the transport. It is the answer to the only request that can reach
 * it, and that request is always a misconfiguration:
 *
 *  - In a deployment, arriving here means the reverse proxy is not forwarding this path,
 *    and the buyer's socket is failing with a handshake error that says nothing. The 503
 *    below says what is actually wrong and what to configure.
 *  - In development, arriving here means something asked this origin for the socket rather
 *    than the gateway -- a stale build with the wrong `NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN`,
 *    or a curl. The reply names the address that does work.
 *
 * A 404 would be the alternative, and a 404 is a lie by omission: the path is real, it is
 * simply served by something that is not running.
 */
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GATEWAY_ORIGIN = (process.env.VOICE_GATEWAY_URL ?? "http://127.0.0.1:8100").replace(/\/+$/, "");

export function GET(): NextResponse {
  return NextResponse.json(
    {
      type: "about:blank",
      title: "This path is a WebSocket, served by the reverse proxy",
      status: 503,
      detail:
        `\`/api/voice/stream\` is upgraded to the voice gateway by the reverse proxy in ` +
        `front of this app; it is not served by the application itself. Reaching this ` +
        `response means no reverse proxy claimed the path. In a deployment, forward it to ` +
        `the gateway with upgrade headers intact. In local development the browser ` +
        `connects to the gateway directly ` +
        `at ${GATEWAY_ORIGIN.replace(/^http/, "ws")}/v1/voice/stream, which is the origin ` +
        `\`lib/security/csp\` names in \`connect-src\` outside production.`,
    },
    {
      status: 503,
      headers: { "Content-Type": "application/problem+json", "Cache-Control": "no-store" },
    },
  );
}
