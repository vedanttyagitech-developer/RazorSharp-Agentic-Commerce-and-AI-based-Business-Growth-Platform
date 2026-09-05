/**
 * The browser socket factory: a ticket, then a socket.
 *
 * The property worth a test here is the one that costs nothing to get wrong and is
 * invisible when you do: a ticket is consumed by its first redeem, so every connection --
 * including every reconnect -- must mint its own. Reusing one leaves the gateway refusing
 * `ticket_unknown` while the session backs off against a service that is working, and
 * nothing in the UI can tell that apart from the gateway being down.
 *
 * The second is that a mint which fails still reports a close. The session's whole
 * reconnect and notice machinery hangs off `onClose`; a mint failure that reported nothing
 * would leave the panel saying "Connecting" forever.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mintVoiceTicket, ticketedSocket } from "../session";
import type { VoiceSocketHandlers } from "../session";

class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly instances: FakeWebSocket[] = [];

  binaryType = "";
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string | ArrayBuffer>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly sent: Array<string | ArrayBuffer> = [];
  closed = false;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
  }
}

function handlers(): VoiceSocketHandlers & { opens: number; closes: number } {
  const record = {
    opens: 0,
    closes: 0,
    onOpen() {
      record.opens += 1;
    },
    onText() {},
    onBinary() {},
    onClose() {
      record.closes += 1;
    },
  };
  return record;
}

/** Let the mint promise and its continuation settle. */
function settle(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

const original = globalThis.WebSocket;

beforeEach(() => {
  FakeWebSocket.instances.length = 0;
  (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
});

afterEach(() => {
  (globalThis as { WebSocket: unknown }).WebSocket = original;
  vi.restoreAllMocks();
});

describe("ticketedSocket", () => {
  it("puts the minted ticket in the query string of the gateway URL", async () => {
    const connect = ticketedSocket(async () => "tkt-one");
    const record = handlers();

    connect("ws://127.0.0.1:8100/v1/voice/stream", record);
    await settle();

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toBe(
      "ws://127.0.0.1:8100/v1/voice/stream?ticket=tkt-one",
    );
    expect(FakeWebSocket.instances[0].binaryType).toBe("arraybuffer");
  });

  it("mints a FRESH ticket for every connection, because one redeem spends it", async () => {
    let issued = 0;
    const connect = ticketedSocket(async () => `tkt-${++issued}`);

    connect("ws://gateway.test/v1/voice/stream", handlers());
    await settle();
    // What a reconnect does: the session calls the factory again with the same base URL.
    connect("ws://gateway.test/v1/voice/stream", handlers());
    await settle();

    expect(FakeWebSocket.instances.map((socket) => socket.url)).toEqual([
      "ws://gateway.test/v1/voice/stream?ticket=tkt-1",
      "ws://gateway.test/v1/voice/stream?ticket=tkt-2",
    ]);
  });

  it("reports a mint failure as a close, so the session backs off and says so", async () => {
    const connect = ticketedSocket(async () => {
      throw new Error("the storefront refused a voice ticket (503)");
    });
    const record = handlers();

    connect("ws://gateway.test/v1/voice/stream", record);
    await settle();

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(record.closes).toBe(1);
    expect(record.opens).toBe(0);
  });

  it("does not open a socket for a session that stopped while the mint was in flight", async () => {
    let release: (ticket: string) => void = () => {};
    const connect = ticketedSocket(
      () =>
        new Promise<string>((resolve) => {
          release = resolve;
        }),
    );
    const record = handlers();

    const socket = connect("ws://gateway.test/v1/voice/stream", record);
    socket.close(); // the buyer closed the panel before the ticket came back
    release("tkt-late");
    await settle();

    // A socket opened here would leave the microphone's peer alive after the panel closed.
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(record.opens).toBe(0);
  });

  it("drops sends that arrive before the socket is open rather than throwing", async () => {
    const connect = ticketedSocket(async () => "tkt-one");
    const socket = connect("ws://gateway.test/v1/voice/stream", handlers());

    expect(() => socket.send("{}")).not.toThrow();
    await settle();
    expect(() => socket.send("{}")).not.toThrow();
    expect(FakeWebSocket.instances[0].sent).toHaveLength(0);

    FakeWebSocket.instances[0].open();
    socket.send("{}");
    expect(FakeWebSocket.instances[0].sent).toEqual(["{}"]);
  });

  it("forwards the socket's own open and close to the session", async () => {
    const connect = ticketedSocket(async () => "tkt-one");
    const record = handlers();
    connect("ws://gateway.test/v1/voice/stream", record);
    await settle();

    FakeWebSocket.instances[0].open();
    expect(record.opens).toBe(1);
    FakeWebSocket.instances[0].onclose?.();
    expect(record.closes).toBe(1);
  });
});

describe("mintVoiceTicket", () => {
  it("posts to the storefront with the session cookie and returns only the ticket", async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      new Response(
        JSON.stringify({
          ticket: "tkt-abc",
          expires_in_s: 60,
          session_id: "vs_1",
          speech_available: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(mintVoiceTicket("/api/voice/tickets")).resolves.toBe("tkt-abc");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/voice/tickets");
    expect(init?.method).toBe("POST");
    // The cookie IS the credential. A fetch that dropped it would mint for a stranger.
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
  });

  it("throws when the storefront refuses, so the factory can report a close", async () => {
    vi.stubGlobal("fetch", async () => new Response("{}", { status: 503 }));
    await expect(mintVoiceTicket("/api/voice/tickets")).rejects.toThrow(/503/);
  });

  it("throws on a well-formed reply that carries no ticket", async () => {
    vi.stubGlobal("fetch", async () =>
      new Response(JSON.stringify({ expires_in_s: 60 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(mintVoiceTicket("/api/voice/tickets")).rejects.toThrow(/no voice ticket/);
  });
});
