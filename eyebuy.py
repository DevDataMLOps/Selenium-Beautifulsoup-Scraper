"""Scrape EyeBuyDirect eyeglass listings into CSV and JSON files.

The scraper uses Selenium for the React-rendered catalogue and BeautifulSoup for
parsing. Output is flushed after every page so an interrupted run keeps all
completed pages.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_URL = "https://www.eyebuydirect.com"
LISTING_PATH = "/eyeglasses"
SOURCE = "eyebuydirect"
OUTPUT_BASENAME = "eyebuydirect_data"
DEFAULT_MAX_PAGES = 10
DEFAULT_WAIT_TIMEOUT = 20
DEFAULT_SCROLL_PASSES = 12
DEFAULT_GRID_URL = os.getenv("SELENIUM_GRID_URL", "http://127.0.0.1:4444")

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

# These attributes are more stable than the generated CSS-module hashes.
PRODUCT_CARD_SELECTOR = 'div[role="region"][aria-label^="Product "]'
NAME_LINK_SELECTOR = '[class*="__item-name"] a[data-inp="plp-product-click"]'
CURRENT_PRICE_SELECTOR = 'ins[class*="__price"]'
FORMER_PRICE_SELECTOR = 'del[class*="__retail"]'
DISCOUNT_SELECTOR = '[class*="__tag-off"]'
BRAND_IMAGE_SELECTOR = '[class*="__item-brands"] img[alt^="tag-"]'

IGNORED_BADGES = {
    "ai-glasses",
    "kids",
    "new",
    "sale",
    "speed",
    "two-day delivery",
}


def collapse_whitespace(value: str | None) -> str | None:
    """Return clean text, preserving a real null for blank values."""
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def node_text(node: Tag | None) -> str | None:
    """Extract normalized visible text from a BeautifulSoup node."""
    if node is None:
        return None
    return collapse_whitespace(node.get_text(" ", strip=True))


def price_to_float(raw: str | None) -> float | None:
    """Convert a displayed price such as '$1,249.50' to a float."""
    if not raw:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    return float(match.group(0).replace(",", "")) if match else None


def discount_text(raw: str | None) -> str | None:
    """Normalize '50% OFF' to the input contract's '50%' representation."""
    if not raw:
        return None
    match = re.search(r"\d+(?:\.\d+)?\s*%", raw)
    return re.sub(r"\s+", "", match.group(0)) if match else None


def infer_discount(former_price: float | None, current_price: float | None) -> str | None:
    """Calculate a discount only when the catalogue does not display one."""
    if former_price is None or current_price is None or former_price <= current_price:
        return None
    percent = round((former_price - current_price) / former_price * 100)
    return f"{percent}%"


def extract_brand(card: Tag) -> str | None:
    """Read designer branding; house frames intentionally retain a null brand."""
    for image in card.select(BRAND_IMAGE_SELECTOR):
        candidate = re.sub(
            r"^tag-", "", image.get("alt", ""), flags=re.IGNORECASE
        ).strip()
        if candidate and candidate.casefold() not in IGNORED_BADGES:
            return candidate
    return None


def remove_brand_prefix(name: str | None, brand: str | None) -> str | None:
    """Remove a designer brand repeated at the start of the model name."""
    if not name or not brand:
        return name
    pattern = rf"^{re.escape(brand)}\s*(?:[-:|–—]\s*)?"
    stripped = re.sub(pattern, "", name, count=1, flags=re.IGNORECASE).strip()
    return stripped or name


def listing_url(page_number: int) -> str:
    """Build EyeBuyDirect's path-based catalogue URL."""
    path = LISTING_PATH if page_number == 1 else f"/eyeglasses-page-{page_number}"
    return urljoin(BASE_URL, path)


def parse_products(
    html: str,
    page_number: int,
    seen_links: set[str] | None = None,
) -> tuple[list[dict], int]:
    """Parse one rendered catalogue page and return new rows plus card count."""
    seen_links = seen_links if seen_links is not None else set()
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(PRODUCT_CARD_SELECTOR)
    rows: list[dict] = []

    for card in cards:
        name_link = card.select_one(NAME_LINK_SELECTOR)
        if name_link is None:
            continue

        product_link = name_link.get("href")
        product_link = urljoin(BASE_URL, product_link) if product_link else None
        if not product_link or product_link in seen_links:
            continue

        name = collapse_whitespace(name_link.get("title")) or node_text(name_link)
        brand = extract_brand(card)
        name = remove_brand_prefix(name, brand)

        current_price = price_to_float(node_text(card.select_one(CURRENT_PRICE_SELECTOR)))
        former_price = price_to_float(node_text(card.select_one(FORMER_PRICE_SELECTOR)))
        discount = discount_text(node_text(card.select_one(DISCOUNT_SELECTOR)))
        discount = discount or infer_discount(former_price, current_price)

        if not name:
            continue

        seen_links.add(product_link)
        rows.append(
            {
                "source": SOURCE,
                "brand": brand,
                "name": name,
                "former_price": former_price,
                "current_price": current_price,
                "discount": discount,
                "product_link": product_link,
                "page": page_number,
            }
        )

    return rows, len(cards)


def initialize_output_files(output_dir: Path) -> tuple[Path, Path]:
    """Start a fresh CSV and JSON output pair."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{OUTPUT_BASENAME}.csv"
    json_path = output_dir / f"{OUTPUT_BASENAME}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump([], handle)

    return csv_path, json_path


def flush_page(
    page_rows: Iterable[dict],
    all_rows: list[dict],
    csv_path: Path,
    json_path: Path,
) -> int:
    """Persist one page to CSV and the complete run-so-far to JSON."""
    rows = list(page_rows)
    if not rows:
        return 0

    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=FIELDNAMES).writerows(rows)

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(all_rows, handle, indent=2, ensure_ascii=False)

    return len(rows)


def version_key(value: str) -> tuple[int, ...]:
    """Convert a dotted browser version to a sortable integer tuple."""
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return (0,)


def find_portable_browser_pair() -> tuple[Path, Path]:
    """Return the newest matching local Chrome-for-Testing browser and driver."""
    override_browser = os.getenv("CHROME_FOR_TESTING_BINARY")
    override_driver = os.getenv("CHROME_FOR_TESTING_DRIVER")
    if override_browser and override_driver:
        browser = Path(override_browser)
        driver = Path(override_driver)
        if browser.is_file() and driver.is_file():
            return browser, driver
        raise FileNotFoundError(
            "CHROME_FOR_TESTING_BINARY and CHROME_FOR_TESTING_DRIVER must point "
            "to existing executable files."
        )

    pairs: list[tuple[tuple[int, ...], Path, Path]] = []

    local_runtime_root = (
        Path.home() / "Documents" / "Codex" / "browser-runtimes" / "chrome-for-testing"
    )
    for runtime_dir in local_runtime_root.glob("*"):
        browser = runtime_dir / "chrome" / "chrome.exe"
        driver = runtime_dir / "chromedriver" / "chromedriver.exe"
        if browser.is_file() and driver.is_file():
            pairs.append((version_key(runtime_dir.name), browser, driver))

    for browser in PROJECT_DIR.glob("chrome/win64/*/chrome.exe"):
        version = browser.parent.name
        driver = PROJECT_DIR / "chromedriver" / "win64" / version / "chromedriver.exe"
        if driver.is_file():
            pairs.append((version_key(version), browser, driver))

    if pairs:
        _, browser, driver = max(pairs, key=lambda item: item[0])
        return browser, driver

    raise FileNotFoundError(
        "A matching portable browser pair was not found under chrome/win64/ "
        "and chromedriver/win64/."
    )


def build_driver(
    start_url: str,
    headless: bool = True,
    grid_url: str | None = DEFAULT_GRID_URL,
) -> WebDriver:
    """Start Chrome through Selenium Grid, or use the local portable fallback."""
    for variable in ("NO_PROXY", "no_proxy"):
        entries = [item for item in os.environ.get(variable, "").split(",") if item]
        for local_host in ("localhost", "127.0.0.1"):
            if local_host not in entries:
                entries.append(local_host)
        os.environ[variable] = ",".join(entries)

    options = Options()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--window-size=1920,3000")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")

    if grid_url:
        print(f"Connecting to Selenium Grid at {grid_url}")
        driver = webdriver.Remote(command_executor=grid_url, options=options)
    else:
        browser_binary, driver_binary = find_portable_browser_pair()
        log_dir = PROJECT_DIR / "extracted_data"
        log_dir.mkdir(parents=True, exist_ok=True)
        driver_log = log_dir / "chromedriver.log"
        options.binary_location = str(browser_binary)
        options.add_argument("--remote-debugging-pipe")

        version = browser_binary.parent.parent.name
        print(f"Starting portable Chrome for Testing {version}")
        service = Service(
            executable_path=str(driver_binary),
            service_args=["--verbose"],
            log_output=str(driver_log),
        )
        driver = webdriver.Chrome(service=service, options=options)

    driver.get(start_url)
    return driver


def close_driver(driver: WebDriver) -> None:
    """Close the Selenium browser session."""
    driver.quit()


def save_page_diagnostics(
    driver: WebDriver,
    output_dir: Path,
    page_number: int,
) -> tuple[Path, Path]:
    """Save the rendered HTML and a screenshot when expected cards are absent."""
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"eyebuydirect_page_{page_number}_diagnostic.html"
    screenshot_path = output_dir / f"eyebuydirect_page_{page_number}_diagnostic.png"
    html_path.write_text(driver.page_source, encoding="utf-8")
    driver.save_screenshot(str(screenshot_path))
    return html_path, screenshot_path


def page_is_access_denied(driver: WebDriver) -> bool:
    """Return True when the remote site explicitly rejects the browser session."""
    title = driver.title.casefold()
    source_start = driver.page_source[:2000].casefold()
    return "access denied" in title or "access denied" in source_start


def dismiss_cookie_banner(driver: WebDriver) -> None:
    """Dismiss a cookie notice when it obscures the page; absence is harmless."""
    labels = ("Close & Continue", "Accept All", "Accept", "Got it", "Continue")
    for label in labels:
        try:
            buttons = driver.find_elements(
                By.XPATH,
                f"//button[contains(normalize-space(.), '{label}')]",
            )
            if buttons and buttons[0].is_displayed():
                buttons[0].click()
                return
        except WebDriverException:
            continue


def scroll_until_stable(
    driver: WebDriver,
    max_passes: int,
) -> int:
    """Trigger lazy rendering until the card count is stable twice."""
    previous_count = -1
    stable_passes = 0

    for _ in range(max_passes):
        current_count = len(driver.find_elements(By.CSS_SELECTOR, PRODUCT_CARD_SELECTOR))
        if current_count == previous_count:
            stable_passes += 1
            if stable_passes >= 2:
                return current_count
        else:
            stable_passes = 0
            previous_count = current_count

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)

    return len(driver.find_elements(By.CSS_SELECTOR, PRODUCT_CARD_SELECTOR))


def scrape(
    max_pages: int,
    output_dir: Path,
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT,
    scroll_passes: int = DEFAULT_SCROLL_PASSES,
    headless: bool = True,
    grid_url: str | None = DEFAULT_GRID_URL,
) -> tuple[Path, Path, int]:
    """Run the scraper and return output paths plus the number of rows."""
    driver: WebDriver | None = None
    all_rows: list[dict] = []
    seen_links: set[str] = set()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{OUTPUT_BASENAME}.csv"
    json_path = output_dir / f"{OUTPUT_BASENAME}.json"
    outputs_initialized = False

    try:
        for page_number in range(1, max_pages + 1):
            url = listing_url(page_number)
            print(f"\n[page {page_number}] {url}")
            driver = build_driver(url, headless=headless, grid_url=grid_url)

            if page_is_access_denied(driver):
                html_path, screenshot_path = save_page_diagnostics(
                    driver,
                    output_dir,
                    page_number,
                )
                print(
                    f"[page {page_number}] EyeBuyDirect denied this Selenium "
                    "session; stopping without attempting to bypass the site."
                )
                print(f"[page {page_number}] diagnostic HTML: {html_path}")
                print(f"[page {page_number}] diagnostic screenshot: {screenshot_path}")
                break

            try:
                WebDriverWait(driver, wait_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, PRODUCT_CARD_SELECTOR))
                )
            except TimeoutException:
                print(f"[page {page_number}] no product cards rendered; stopping.")
                html_path, screenshot_path = save_page_diagnostics(
                    driver,
                    output_dir,
                    page_number,
                )
                print(f"[page {page_number}] diagnostic HTML: {html_path}")
                print(f"[page {page_number}] diagnostic screenshot: {screenshot_path}")
                break

            dismiss_cookie_banner(driver)

            rendered_count = scroll_until_stable(driver, scroll_passes)
            page_rows, card_count = parse_products(
                driver.page_source,
                page_number,
                seen_links,
            )
            if page_rows and not outputs_initialized:
                csv_path, json_path = initialize_output_files(output_dir)
                outputs_initialized = True
            all_rows.extend(page_rows)
            saved = flush_page(page_rows, all_rows, csv_path, json_path)

            print(
                f"[page {page_number}] rendered {rendered_count}; "
                f"parsed {card_count}; saved {saved}; total {len(all_rows)}"
            )

            if card_count == 0 or saved == 0:
                print("No new products found; reached the end of the catalogue.")
                break

            close_driver(driver)
            driver = None
            time.sleep(1)

        return csv_path, json_path, len(all_rows)
    finally:
        if driver is not None:
            close_driver(driver)
            print("WebDriver closed.")


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
        default=int(os.getenv("EYEBUY_MAX_PAGES", DEFAULT_MAX_PAGES)),
        help=f"catalogue pages to scrape (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for CSV and JSON output",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser through Grid noVNC instead of using headless mode",
    )
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument(
        "--grid-url",
        default=DEFAULT_GRID_URL,
        help=f"Selenium Grid endpoint (default: {DEFAULT_GRID_URL})",
    )
    connection.add_argument(
        "--local-browser",
        action="store_true",
        help="use the local portable Chrome/ChromeDriver pair instead of Grid",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        csv_path, json_path, row_count = scrape(
            max_pages=args.max_pages,
            output_dir=args.output_dir,
            headless=not args.headed,
            grid_url=None if args.local_browser else args.grid_url,
        )
    except Exception as exc:
        # Driver-manager and local-driver connection failures can surface through
        # urllib3 rather than Selenium's WebDriverException hierarchy.
        print(f"Scraper failed ({type(exc).__name__}): {exc}")
        return 1

    if row_count:
        print(f"\nSaved {row_count} records to:")
    else:
        print("\nNo new records collected; existing exports were preserved at:")
    print(f"  {csv_path}")
    print(f"  {json_path}")
    return 0 if row_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
