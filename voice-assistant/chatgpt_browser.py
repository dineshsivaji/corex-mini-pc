#!/usr/bin/env python3
"""
Drive chatgpt.com in a real browser window (logged out), send a question,
and print the reply.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python chatgpt_browser.py "How many appendages does a squid have?"
    python chatgpt_browser.py            # interactive mode, same chat session

Notes:
  - Runs a visible browser window. If a CAPTCHA or popup appears, deal with it
    in the window and the script will carry on.
  - The page's HTML changes often; if it breaks, update the SELECTORS below.
"""

import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://chatgpt.com/"
PROFILE_DIR = "./.chatgpt-browser-profile"  # keeps cookies/consent between runs (no login)

SELECTORS = {
    # New composer is a plain <textarea name="prompt">; old one was #prompt-textarea.
    "input": 'textarea[name="prompt"]:visible, textarea[aria-label="Chat with ChatGPT"]:visible, #prompt-textarea:visible',
    "send": 'button[data-testid="send-button"]',
    # Each reply is <li data-message-role="assistant">; it gains data-message-complete when done.
    "assistant": 'li[data-message-role="assistant"], [data-message-author-role="assistant"]',
    "complete_attr": "data-message-complete",
    "reply_text": "[data-assistant-markdown-root]",
}

PREFIX = (
    "Answer in 2-3 short, simple sentences for a 5-year-old, under 45 words, "
    "no lists or markdown. Question: "
)


def dismiss_popups(page) -> None:
    """Close the 'log in / sign up' nag if it shows up."""
    for text in ("Stay logged out", "Maybe later", "Dismiss"):
        btn = page.get_by_text(text, exact=True)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            time.sleep(0.5)


def wait_for_reply(page, previous_count: int, timeout_s: int = 90) -> str:
    """Wait until a new assistant message appears and stops changing."""
    msgs = page.locator(SELECTORS["assistant"])
    deadline = time.time() + timeout_s

    while msgs.count() <= previous_count:
        if time.time() > deadline:
            raise TimeoutError("No reply appeared.")
        dismiss_popups(page)
        time.sleep(0.3)

    reply = msgs.last
    last_text, stable_since = "", time.time()
    while time.time() < deadline:
        body = reply.locator(SELECTORS["reply_text"])
        text = (body.first.inner_text() if body.count() else reply.inner_text()).strip()
        done = reply.get_attribute(SELECTORS["complete_attr"]) is not None
        if text != last_text:
            last_text, stable_since = text, time.time()
        # Done when the page marks it complete, or (fallback) text unchanged for 3 s.
        quiet = time.time() - stable_since
        if text and ((done and quiet > 1) or quiet > 3):
            return " ".join(text.split())  # tidy whitespace between streamed words
        time.sleep(0.3)
    return last_text  # timed out mid-stream: return what we have


def ask(page, question: str) -> str:
    dismiss_popups(page)
    box = page.locator(SELECTORS["input"]).first
    box.wait_for(state="visible", timeout=30_000)

    before = page.locator(SELECTORS["assistant"]).count()
    box.click()
    box.fill(PREFIX + question)

    send = page.locator(SELECTORS["send"])
    try:
        send.wait_for(state="visible", timeout=5_000)
        send.click()
    except PWTimeout:
        box.press("Enter")

    return wait_for_reply(page, before)


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        try:
            if len(sys.argv) > 1:
                print(ask(page, " ".join(sys.argv[1:])))
            else:
                print("Connected to chatgpt.com. Ctrl+C to quit.")
                while True:
                    q = input("\nYou: ").strip()
                    if q:
                        print("ChatGPT:", ask(page, q))
        except (KeyboardInterrupt, EOFError):
            print()
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
