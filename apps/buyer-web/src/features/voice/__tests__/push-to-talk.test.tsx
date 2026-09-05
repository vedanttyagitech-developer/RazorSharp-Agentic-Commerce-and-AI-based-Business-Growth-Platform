/**
 * The talk control, from the keyboard and from a screen reader.
 *
 * A push-to-talk that only works with a mouse held down is a push-to-talk that a good part
 * of the people who most benefit from talking to a shop cannot use at all.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PushToTalk } from "../push-to-talk";
import type { MicState } from "../session";

afterEach(cleanup);

function Harness({
  micState = "live",
  assistantSpeaking = false,
  disabled = false,
  onChange,
}: {
  micState?: MicState;
  assistantSpeaking?: boolean;
  disabled?: boolean;
  onChange?: (on: boolean) => void;
}) {
  const [transmitting, setTransmitting] = useState(false);
  return (
    <PushToTalk
      transmitting={transmitting}
      onTransmitChange={(on) => {
        setTransmitting(on);
        onChange?.(on);
      }}
      level={0.42}
      micState={micState}
      assistantSpeaking={assistantSpeaking}
      disabled={disabled}
    />
  );
}

describe("PushToTalk", () => {
  it("is labelled, and says which gesture it wants", () => {
    render(<Harness />);
    const button = screen.getByRole("button", { name: /hold to talk/i });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.getAttribute("aria-describedby")).toBe("voice-ptt-status");
  });

  it("transmits while a talk key is held and stops when it is released", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const button = screen.getByRole("button", { name: /hold to talk/i });

    fireEvent.keyDown(button, { key: " " });
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: /release to stop/i })).toBeTruthy();

    fireEvent.keyUp(button, { key: " " });
    expect(screen.getByRole("button", { name: /hold to talk/i }).getAttribute("aria-pressed")).toBe(
      "false",
    );
    expect(onChange.mock.calls.map((call) => call[0])).toEqual([true, false]);
  });

  it("works with Enter as well as Space, and ignores auto-repeat", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const button = screen.getByRole("button", { name: /hold to talk/i });

    fireEvent.keyDown(button, { key: "Enter" });
    fireEvent.keyDown(button, { key: "Enter", repeat: true });
    fireEvent.keyUp(button, { key: "Enter" });

    // One start and one stop, not one per repeat.
    expect(onChange.mock.calls.map((call) => call[0])).toEqual([true, false]);
  });

  it("stops transmitting when focus leaves the control mid-hold", () => {
    render(<Harness />);
    const button = screen.getByRole("button", { name: /hold to talk/i });
    fireEvent.keyDown(button, { key: " " });
    expect(button.getAttribute("aria-pressed")).toBe("true");

    fireEvent.blur(button);
    expect(
      screen.getByRole("button", { name: /hold to talk/i }).getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("carries a visible focus ring rather than relying on the browser default", () => {
    render(<Harness />);
    const button = screen.getByRole("button", { name: /hold to talk/i });
    expect(button.className).toContain("focus-visible:outline-2");
  });

  it("announces its state in a live region", () => {
    render(<Harness />);
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent).toMatch(/not sending/i);

    fireEvent.keyDown(screen.getByRole("button", { name: /hold to talk/i }), { key: " " });
    expect(screen.getByRole("status").textContent).toMatch(/sending your voice/i);
  });

  it("shows the level meter", () => {
    render(<Harness />);
    const meter = screen.getByRole("meter", { name: /microphone level/i });
    expect(meter.getAttribute("aria-valuenow")).toBe("42");
  });

  it("says plainly that the microphone is muted while the assistant speaks", () => {
    render(<Harness assistantSpeaking />);
    expect(screen.getByText(/microphone muted while she speaks/i)).toBeTruthy();
    expect(screen.getByRole("status").textContent).toMatch(/muted until she finishes/i);
  });

  it("stays in the tab order when it cannot be used, and says why", () => {
    render(<Harness micState="denied" />);
    const button = screen.getByRole("button", { name: /hold to talk/i });
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("status").textContent).toMatch(/still type/i);

    fireEvent.keyDown(button, { key: " " });
    expect(button.getAttribute("aria-pressed")).toBe("false");
  });
});
