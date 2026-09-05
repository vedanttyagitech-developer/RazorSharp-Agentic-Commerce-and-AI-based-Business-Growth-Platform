import { render, screen, fireEvent, act, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AiShoppingSlider, AI_FEATURE_CARDS } from "./ai-shopping-slider";
import { PromoBanners } from "./promo-banners";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("AiShoppingSlider", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("renders the AI shopping slider with step 01 and conversational cart details", () => {
    render(<AiShoppingSlider />);

    expect(screen.getByText("How Zepto AI Works")).toBeTruthy();
    expect(screen.getByText("01 / 05")).toBeTruthy();
    expect(screen.getByText("Talk or Type — Cart Built in Seconds")).toBeTruthy();
    expect(
      screen.getByText(/Say '2 packets milk and brown bread' or paste dinner lists/i)
    ).toBeTruthy();
    expect(screen.getByText("⚡ 1-Tap Staging vs 15 Manual Searches")).toBeTruthy();
  });

  it("navigates forward and backward using arrow controls", () => {
    render(<AiShoppingSlider />);

    const nextBtn = screen.getByLabelText("Next slide");
    const prevBtn = screen.getByLabelText("Previous slide");

    // Click Next -> Slide 2 (Dietary)
    fireEvent.click(nextBtn);
    expect(screen.getByText("02 / 05")).toBeTruthy();
    expect(screen.getByText("Dietary Reasoning & Complete Recipe Kits")).toBeTruthy();

    // Click Next -> Slide 3 (Price Shift Shield)
    fireEvent.click(nextBtn);
    expect(screen.getByText("03 / 05")).toBeTruthy();
    expect(screen.getByText("Zero Surprise Charges or Price Bumps")).toBeTruthy();

    // Click Prev -> Slide 2
    fireEvent.click(prevBtn);
    expect(screen.getByText("02 / 05")).toBeTruthy();
  });

  it("jumps to specific slide when clicking pagination indicator dots", () => {
    render(<AiShoppingSlider />);

    const slide4Btn = screen.getByLabelText(
      `Go to slide 4: ${AI_FEATURE_CARDS[3].title}`
    );
    fireEvent.click(slide4Btn);

    expect(screen.getByText("04 / 05")).toBeTruthy();
    expect(screen.getByText("AI Drafts Proposals, You Retain Authority")).toBeTruthy();
  });

  it("auto-advances slides every 4.5 seconds and pauses on hover", () => {
    render(<AiShoppingSlider />);

    expect(screen.getByText("01 / 05")).toBeTruthy();

    // Advance 4500ms -> slide 2
    act(() => {
      vi.advanceTimersByTime(4500);
    });
    expect(screen.getByText("02 / 05")).toBeTruthy();

    // Hover over container to pause
    const region = screen.getByRole("region", { name: /How AI-Assisted Shopping Works/i });
    fireEvent.mouseEnter(region);

    // Advance another 4500ms while paused -> should stay on slide 2
    act(() => {
      vi.advanceTimersByTime(4500);
    });
    expect(screen.getByText("02 / 05")).toBeTruthy();

    // Mouse leave -> resumes timer
    fireEvent.mouseLeave(region);
    act(() => {
      vi.advanceTimersByTime(4500);
    });
    expect(screen.getByText("03 / 05")).toBeTruthy();
  });

  it("dispatches open-zepto-ai custom event when action button is clicked", () => {
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");
    render(<AiShoppingSlider />);

    const actionBtn = screen.getByText('Try: "2 packet doodh add karo"');
    fireEvent.click(actionBtn);

    expect(dispatchSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "open-zepto-ai",
        detail: { prompt: "2 packet doodh add karo" },
      })
    );
  });
});

describe("PromoBanners", () => {
  it("completely replaces Paan Corner with the AI Shopping Slider and retains Banner 1", () => {
    render(<PromoBanners />);

    // Zero fees promo is retained
    expect(screen.getByText("ALL")).toBeTruthy();
    expect(screen.getByText("NEW ZEPTO EXPERIENCE")).toBeTruthy();
    expect(screen.getByText("₹0 FEES")).toBeTruthy();

    // Paan corner is completely removed
    expect(screen.queryByText(/PAAN CORNER/i)).toBeNull();
    expect(screen.queryByText(/smoking accessories/i)).toBeNull();
    expect(screen.queryByText(/stash-pro/i)).toBeNull();

    // AI shopping slider is rendered in its place
    expect(screen.getByText("How Zepto AI Works")).toBeTruthy();
  });
});
