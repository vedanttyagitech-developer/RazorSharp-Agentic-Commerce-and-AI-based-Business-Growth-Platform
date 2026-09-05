/**
 * The launcher's memory: the box opens itself on the home shelf, and only until closed.
 *
 * The launcher is mounted once in the root layout and the checkout opens in a NEW
 * document, so "the buyer closed it" cannot live in component state -- every full page
 * load started from nothing and reopened the box, over the approval card the buyer had
 * just closed it to read. The property under test is the one that fixes that: a close is
 * remembered for the tab, an auto-open happens only on `/`, and the launcher button
 * still opens the box on demand whatever the memory says.
 *
 * The panel is stubbed to a dialog with a close control, because the real one wants the
 * basket provider, the API client and a sound card, and none of that is what this file
 * is about. The stub honours the one contract the launcher relies on: `open` decides
 * whether anything is drawn, and `onClose` is what the close control calls.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RazorAILauncher } from "@/features/agent/launcher";

const DISMISSED_KEY = "razorai.dismissed";

const mocks = vi.hoisted(() => ({
  pathname: "/" as string | null,
  wide: true,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
}));

vi.mock("@/features/agent/razorai-panel", () => ({
  RazorAIMark: () => null,
  RazorAIPanel: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div role="dialog" aria-label="RazorAI">
        <button type="button" onClick={onClose}>
          Close RazorAI
        </button>
      </div>
    ) : null,
}));

beforeEach(() => {
  mocks.pathname = "/";
  mocks.wide = true;
  window.sessionStorage.clear();
  // jsdom has no `matchMedia`; the launcher's width check reads this one.
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: mocks.wide,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as MediaQueryList,
  });
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** The auto-open is a zero-delay timer, so "the page is up" is the timers having run. */
function pageUp() {
  act(() => {
    vi.advanceTimersByTime(0);
  });
}

function dialog(): HTMLElement | null {
  return screen.queryByRole("dialog");
}

describe("RazorAILauncher", () => {
  it("opens itself on the home route when the buyer has not dismissed it", () => {
    render(<RazorAILauncher />);
    expect(dialog()).toBeNull();

    pageUp();

    expect(dialog()).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open RazorAI" }).getAttribute("aria-expanded")).toBe(
      "true",
    );
  });

  it("stays closed off the home route, and still opens from the launcher button", () => {
    mocks.pathname = "/checkout/01a070e9-c3d9-7d09-b638-a3082ede458d";
    render(<RazorAILauncher />);

    pageUp();
    expect(dialog()).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Open RazorAI" }));
    expect(dialog()).toBeTruthy();
  });

  it("stays closed once the buyer has dismissed it in this tab", () => {
    window.sessionStorage.setItem(DISMISSED_KEY, "1");
    render(<RazorAILauncher />);

    pageUp();
    expect(dialog()).toBeNull();

    // The memory is about opening itself, not about opening at all.
    fireEvent.click(screen.getByRole("button", { name: "Open RazorAI" }));
    expect(dialog()).toBeTruthy();
  });

  it("remembers a close for the tab, so the next document does not reopen it", () => {
    const { unmount } = render(<RazorAILauncher />);
    pageUp();
    expect(dialog()).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Close RazorAI" }));
    expect(dialog()).toBeNull();
    expect(window.sessionStorage.getItem(DISMISSED_KEY)).toBe("1");

    // A full page load: a fresh mount with nothing but the tab's storage carried over.
    unmount();
    render(<RazorAILauncher />);
    pageUp();
    expect(dialog()).toBeNull();
  });

  it("keeps the launcher on a phone, where the box would cover the shelf", () => {
    mocks.wide = false;
    render(<RazorAILauncher />);

    pageUp();
    expect(dialog()).toBeNull();
  });

  it("treats storage it cannot read as not dismissed, and survives a close it cannot record", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage is disabled");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage is disabled");
    });
    render(<RazorAILauncher />);

    pageUp();
    expect(dialog()).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Close RazorAI" }));
    expect(dialog()).toBeNull();
  });
});
