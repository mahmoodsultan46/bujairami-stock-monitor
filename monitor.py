#!/usr/bin/env python3
"""Monitor FragranceNet for Bujairami Too Damn Good and alert Discord."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TARGET_NAME = "Bujairami Too Damn Good"
TARGET_WORDS = ("bujairami", "too", "damn", "good")
TARGET_SLUG = "bujairami-too-damn-good"
BRAND_URL = "https://www.fragrancenet.com/fragrances/bujairami"
NEW_ARRIVALS_URL = "https://www.fragrancenet.com/ni/fragrances/bujairami?s=new"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STOP_REQUESTED = False


def load_env_file() -> None:
    """Load simple KEY=VALUE settings from the private local .env file."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

STATE_FILE = Path(
    os.getenv("STATE_FILE", str(Path(__file__).with_name("state.json")))
).expanduser()


def env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a whole number, not {raw!r}") from exc
    return max(value, minimum)


CHECK_INTERVAL_SECONDS = env_int("CHECK_INTERVAL_SECONDS", 300, 60)
PAGE_TIMEOUT_MS = env_int("PAGE_TIMEOUT_MS", 45_000, 10_000)
REMINDER_HOURS = env_int("REMINDER_HOURS", 6, 1)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "status": "unknown",
            "product_url": os.getenv("PRODUCT_URL", "").strip(),
            "last_in_stock_alert": None,
            "last_error_alert": None,
            "consecutive_errors": 0,
        }
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if os.getenv("PRODUCT_URL", "").strip():
            state["product_url"] = os.environ["PRODUCT_URL"].strip()
        return state
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning("Could not read state.json; starting fresh: %s", exc)
        return {"status": "unknown", "product_url": "", "consecutive_errors": 0}


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def hours_since(timestamp: str | None) -> float:
    if not timestamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(timestamp)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600
    except ValueError:
        return float("inf")


def send_discord(webhook_url: str, title: str, description: str, color: int) -> None:
    payload = {
        "username": "FragranceNet Stock Watcher",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "timestamp": utc_now(),
            }
        ],
    }
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Discord returned HTTP {exc.code}: {detail}") from exc


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def is_target_link(text: str, href: str) -> bool:
    combined = normalized(f"{text} {href}")
    return TARGET_SLUG in href.lower() or all(word in combined.split() for word in TARGET_WORDS)


def listing_urls() -> list[str]:
    # A newly released product should appear at the top of this sorted page.
    # Keeping this to one request also avoids triggering the retailer's rate limit.
    return [NEW_ARRIVALS_URL]


def load_page(page: Page, url: str) -> None:
    response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if response and response.status >= 400:
        raise RuntimeError(f"FragranceNet returned HTTP {response.status} for {url}")
    page.locator("body").wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)


def discover_product_url(page: Page) -> str | None:
    for url in listing_urls():
        logging.info("Looking for the product on %s", url)
        load_page(page, url)
        links: list[dict[str, str]] = page.locator("a[href]").evaluate_all(
            """els => els.map(a => ({
                text: (a.textContent || '').trim().replace(/\\s+/g, ' '),
                href: a.href || ''
            }))"""
        )
        matches = [item for item in links if is_target_link(item["text"], item["href"])]
        if matches:
            # A detail/variant link is preferable to a brand-filter link.
            matches.sort(
                key=lambda item: (
                    "#" not in item["href"],
                    not any(part in item["href"] for part in ("/cologne/", "/perfume/")),
                    len(item["href"]),
                )
            )
            return matches[0]["href"].split("?")[0]
    return None


def product_is_in_stock(page: Page, product_url: str) -> tuple[bool, str]:
    load_page(page, product_url)
    add_buttons = page.get_by_role("button", name="Add to Bag", exact=True)
    if add_buttons.count() > 0:
        enabled = any(add_buttons.nth(i).is_enabled() for i in range(add_buttons.count()))
        if enabled:
            return True, "An enabled “Add to Bag” button is visible."

    body_text = normalized(page.locator("body").inner_text(timeout=PAGE_TIMEOUT_MS))
    sold_out_phrases = (
        "we apologize we are currently sold out",
        "currently sold out",
        "out of stock",
    )
    if any(phrase in body_text for phrase in sold_out_phrases):
        return False, "The product page says it is sold out."
    return False, "The product page is live, but no enabled “Add to Bag” button was found."


def check_once(browser: Browser, state: dict[str, Any], webhook_url: str, dry_run: bool) -> None:
    page = browser.new_page(user_agent=USER_AGENT, locale="en-US")
    previous_status = state.get("status", "unknown")
    try:
        product_url = state.get("product_url") or discover_product_url(page)
        if not product_url:
            state.update(
                status="not_listed",
                last_checked=utc_now(),
                consecutive_errors=0,
                detail="No matching FragranceNet listing found yet.",
            )
            logging.info("Not listed yet. Next check will run later.")
            return

        if product_url != state.get("product_url"):
            logging.info("Found the product listing: %s", product_url)
            state["product_url"] = product_url

        in_stock, detail = product_is_in_stock(page, product_url)
        new_status = "in_stock" if in_stock else "listed_unavailable"
        state.update(
            status=new_status,
            last_checked=utc_now(),
            detail=detail,
            consecutive_errors=0,
        )

        if in_stock:
            should_alert = previous_status != "in_stock" or hours_since(
                state.get("last_in_stock_alert")
            ) >= REMINDER_HOURS
            if should_alert:
                message = (
                    f"**{TARGET_NAME} appears to be in stock.**\n"
                    f"[Open the product page]({product_url})\n\n"
                    "Stock can move quickly—confirm availability and price before buying."
                )
                if dry_run:
                    logging.info("DRY RUN Discord alert: %s", message)
                else:
                    send_discord(webhook_url, "🚨 In stock on FragranceNet", message, 0x2ECC71)
                    state["last_in_stock_alert"] = utc_now()
                logging.info("IN STOCK: %s", product_url)
            else:
                logging.info("Still in stock; reminder cooldown has not elapsed.")
        else:
            logging.info("Listed but unavailable: %s", detail)
            if previous_status == "not_listed" and not dry_run:
                send_discord(
                    webhook_url,
                    "👀 The listing is live",
                    f"**{TARGET_NAME}** now has a FragranceNet page, but it is not purchasable yet.\n"
                    f"[Open the listing]({product_url})",
                    0xF1C40F,
                )
    finally:
        page.close()


def handle_error(state: dict[str, Any], webhook_url: str, exc: Exception, dry_run: bool) -> None:
    state["consecutive_errors"] = int(state.get("consecutive_errors", 0)) + 1
    state["last_checked"] = utc_now()
    state["last_error"] = f"{type(exc).__name__}: {exc}"
    logging.exception("Check failed")
    if (
        state["consecutive_errors"] >= 3
        and hours_since(state.get("last_error_alert")) >= REMINDER_HOURS
        and not dry_run
    ):
        send_discord(
            webhook_url,
            "⚠️ Stock watcher needs attention",
            f"The last {state['consecutive_errors']} checks failed. Latest error: `{type(exc).__name__}: {exc}`",
            0xE74C3C,
        )
        state["last_error_alert"] = utc_now()


def stop(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--dry-run", action="store_true", help="Never send Discord messages")
    parser.add_argument("--test-discord", action="store_true", help="Send one test alert and exit")
    parser.add_argument("--headed", action="store_true", help="Show the browser while checking")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url and not args.dry_run:
        raise SystemExit("Set DISCORD_WEBHOOK_URL first (see README.md).")
    if args.test_discord:
        send_discord(
            webhook_url,
            "✅ Stock watcher connected",
            f"Discord alerts for **{TARGET_NAME}** are working.",
            0x5865F2,
        )
        logging.info("Test notification sent.")
        return 0

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    state = load_state()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        try:
            while not STOP_REQUESTED:
                try:
                    check_once(browser, state, webhook_url, args.dry_run)
                except (PlaywrightTimeoutError, OSError, RuntimeError) as exc:
                    handle_error(state, webhook_url, exc, args.dry_run)
                finally:
                    save_state(state)

                if args.once:
                    break
                logging.info("Sleeping for %s seconds.", CHECK_INTERVAL_SECONDS)
                for _ in range(CHECK_INTERVAL_SECONDS):
                    if STOP_REQUESTED:
                        break
                    time.sleep(1)
        finally:
            browser.close()
    logging.info("Watcher stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
