"""Scrape FramesDirect eyeglass listings with Selenium Grid and BeautifulSoup."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_URL = "https://www.framesdirect.com"
LISTING_PATH = "/eyeglasses/"
SOURCE = "framesdirect"
OUTPUT_BASENAME = "framesdirect_data"
DEFAULT_MAX_PAGES = 5
DEFAULT_WAIT_TIMEOUT = 30
DEFAULT_GRID_URL = os.getenv("SELENIUM_FIREFOX_GRID_URL", "http://127.0.0.1:4445")

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "extracted_data"

FIELDNAMES = [
    "source",
    "brand",
    "name",
    "former_price",
    "current_price",
    "discount",
    "product_link",
    "page",
]

PRODUCT_CARD_SELECTOR = "div.prod-holder"
BRAND_SELECTOR = ".catalog-name"
NAME_SELECTOR = ".product_name"
SALE_PRICE_SELECTOR = ".salePriceID"
RETAIL_PRICE_SELECTOR = ".retailPriceID"
PRODUCT_LINK_SELECTOR = ".prod-image-holder a[href]"


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and return None for blank values."""
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def node_text(node: Tag | None) -> str | None:
    """Read normalized text from a BeautifulSoup element."""
    return clean_text(node.get_text(" ", strip=True)) if node else None


def price_to_float(raw: str | None) -> float | None:
    """Convert a displayed price into a numeric value."""
    if not raw:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    return float(match.group(0).replace(",", "")) if match else None


def discount_text(former_price: float | None, current_price: float | None) -> str | None:
    """Calculate a rounded percentage discount for sale listings."""
    if (
        former_price is None
        or current_price is None
        or former_price <= 0
        or current_price >= former_price
    ):
        return None
    percent = round((former_price - current_price) / former_price * 100)
    return f"{percent}%"


def listing_url(page_number: int) -> str:
    """Build the verified FramesDirect catalogue URL for a page number."""
    if page_number <= 1:
        return urljoin(BASE_URL, LISTING_PATH)
    return urljoin(BASE_URL, f"{LISTING_PATH}?p={page_number}&type=pagestate")


def parse_products(
    html: str,
    page_number: int,
    seen_links: set[str],
) -> tuple[list[dict], int]:
    """Parse product cards from rendered FramesDirect HTML."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(PRODUCT_CARD_SELECTOR)
    rows: list[dict] = []

    for card in cards:
        brand = node_text(card.select_one(BRAND_SELECTOR))
        name = node_text(card.select_one(NAME_SELECTOR))
        current_price = price_to_float(node_text(card.select_one(SALE_PRICE_SELECTOR)))
        retail_price = price_to_float(node_text(card.select_one(RETAIL_PRICE_SELECTOR)))
        link_node = card.select_one(PRODUCT_LINK_SELECTOR)

        if not name or current_price is None or not link_node:
            continue

        href = clean_text(link_node.get("href"))
        if not href:
            continue
        product_link = urljoin(BASE_URL, href)
        if product_link in seen_links:
            continue

        former_price = (
            retail_price
            if retail_price is not None and retail_price > current_price
            else None
        )
        seen_links.add(product_link)
        rows.append(
            {
                "source": SOURCE,
                "brand": brand,
                "name": name,
                "former_price": former_price,
                "current_price": current_price,
                "discount": discount_text(former_price, current_price),
                "product_link": product_link,
                "page": page_number,
            }
        )

    return rows, len(cards)


def initialize_output_files(output_dir: Path) -> tuple[Path, Path]:
    """Create empty CSV and JSON outputs with the expected schema."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{OUTPUT_BASENAME}.csv"
    json_path = output_dir / f"{OUTPUT_BASENAME}.json"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
    json_path.write_text("[]\n", encoding="utf-8")
    return csv_path, json_path


def flush_rows(rows: list[dict], csv_path: Path, json_path: Path) -> None:
    """Persist all completed rows after each catalogue page."""
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_driver(grid_url: str, headless: bool) -> WebDriver:
    """Connect to the official Firefox Selenium Grid node."""
    for variable in ("NO_PROXY", "no_proxy"):
        entries = [item for item in os.environ.get(variable, "").split(",") if item]
        for local_host in ("localhost", "127.0.0.1"):
            if local_host not in entries:
                entries.append(local_host)
        os.environ[variable] = ",".join(entries)

    options = Options()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("-headless")
    options.add_argument("--width=1920")
    options.add_argument("--height=3000")

    print(f"Connecting to Firefox Selenium Grid at {grid_url}")
    return webdriver.Remote(command_executor=grid_url, options=options)


def page_is_access_denied(driver: WebDriver) -> bool:
    """Detect an explicit access-denied response without trying to evade it."""
    title = driver.title.casefold()
    source_start = driver.page_source[:2000].casefold()
    return "access denied" in title or "access denied" in source_start


def save_diagnostics(
    driver: WebDriver,
    output_dir: Path,
    page_number: int,
) -> tuple[Path, Path]:
    """Save HTML and a screenshot for a failed catalogue page."""
    html_path = output_dir / f"framesdirect_page_{page_number}_diagnostic.html"
    screenshot_path = output_dir / f"framesdirect_page_{page_number}_diagnostic.png"
    html_path.write_text(driver.page_source, encoding="utf-8")
    driver.save_screenshot(str(screenshot_path))
    return html_path, screenshot_path


def scrape(
    max_pages: int,
    output_dir: Path,
    grid_url: str = DEFAULT_GRID_URL,
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT,
    headless: bool = True,
) -> tuple[Path, Path, int]:
    """Scrape catalogue pages and return output paths plus saved row count."""
    csv_path, json_path = initialize_output_files(output_dir)
    rows: list[dict] = []
    seen_links: set[str] = set()
    driver = build_driver(grid_url, headless)

    try:
        driver.set_page_load_timeout(wait_timeout + 15)
        for page_number in range(1, max_pages + 1):
            url = listing_url(page_number)
            print(f"\n[page {page_number}] {url}")
            driver.get(url)

            if page_is_access_denied(driver):
                html_path, screenshot_path = save_diagnostics(
                    driver,
                    output_dir,
                    page_number,
                )
                raise RuntimeError(
                    "FramesDirect denied this Selenium session. "
                    f"Diagnostics: {html_path}, {screenshot_path}"
                )

            try:
                WebDriverWait(driver, wait_timeout).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, f"{PRODUCT_CARD_SELECTOR} {NAME_SELECTOR}")
                    )
                )
            except TimeoutException:
                html_path, screenshot_path = save_diagnostics(
                    driver,
                    output_dir,
                    page_number,
                )
                print(f"[page {page_number}] product cards did not render; stopping.")
                print(f"[page {page_number}] diagnostic HTML: {html_path}")
                print(f"[page {page_number}] diagnostic screenshot: {screenshot_path}")
                break

            page_rows, card_count = parse_products(
                driver.page_source,
                page_number,
                seen_links,
            )
            rows.extend(page_rows)
            flush_rows(rows, csv_path, json_path)
            print(
                f"[page {page_number}] parsed {card_count} cards; "
                f"saved {len(page_rows)} new rows; total {len(rows)}"
            )

            if card_count == 0 or not page_rows:
                print("No new products found; reached the end of the catalogue.")
                break
            time.sleep(1)
    finally:
        driver.quit()
        print("WebDriver closed.")

    return csv_path, json_path, len(rows)


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-pages",
        type=positive_integer,
        default=int(os.getenv("FRAMES_MAX_PAGES", DEFAULT_MAX_PAGES)),
        help=f"catalogue pages to scrape (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for CSV and JSON output",
    )
    parser.add_argument(
        "--grid-url",
        default=DEFAULT_GRID_URL,
        help=f"Firefox Selenium Grid endpoint (default: {DEFAULT_GRID_URL})",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser through Grid noVNC instead of using headless mode",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        csv_path, json_path, row_count = scrape(
            max_pages=args.max_pages,
            output_dir=args.output_dir,
            grid_url=args.grid_url,
            headless=not args.headed,
        )
    except Exception as exc:
        print(f"Scraper failed ({type(exc).__name__}): {exc}")
        return 1

    print(f"\nSaved {row_count} records to:")
    print(f"  {csv_path}")
    print(f"  {json_path}")
    return 0 if row_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
