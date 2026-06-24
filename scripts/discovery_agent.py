#!/usr/bin/env python3
"""
Compare Finance Competitive Intelligence — Discovery Agent
Crawls a competitor's sitemap/nav to build the monitored URL list in Notion.

Usage:
    python discovery_agent.py <competitor_key>

Competitor keys:
    nerdwallet, creditkarma, bankrate, financebuzz, lendingtree, bestmoney, credible
"""

import os
import sys
import re
import argparse
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from notion_client import Client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COMPETITORS = {
    "nerdwallet":   "https://www.nerdwallet.com",
    "creditkarma":  "https://www.creditkarma.com",
    "bankrate":     "https://www.bankrate.com",
    "financebuzz":  "https://www.financebuzz.com",
    "lendingtree":  "https://www.lendingtree.com",
    "bestmoney":    "https://www.bestmoney.com",
    "credible":     "https://www.credible.com",
}

COMPETITOR_DISPLAY_NAMES = {
    "nerdwallet":   "NerdWallet",
    "creditkarma":  "Credit Karma",
    "bankrate":     "Bankrate",
    "financebuzz":  "FinanceBuzz",
    "lendingtree":  "LendingTree",
    "bestmoney":    "BestMoney",
    "credible":     "Credible",
}

# Priority tier 1: product category root pages (exact match)
PRODUCT_ROOTS = [
    "credit-cards",
    "personal-loans",
    "auto-insurance",
    "car-insurance",
    "vehicle-insurance",
    "home-insurance",
    "homeowners-insurance",
    "life-insurance",
    "loans",
]

# Priority tier 2: sub-pages one level under a known product root
# e.g. /credit-cards/rewards/ or /personal-loans/debt-consolidation/
PRODUCT_SUBPAGE_PATTERN = re.compile(
    r"^/(" + "|".join(PRODUCT_ROOTS) + r")/[^/]+/?$"
)

# Priority tier 3: best-of pages at top level or one level deep
BEST_OF_PATTERNS = [
    r"^/best-[^/]+/?$",
    r"^/[^/]+/best-[^/]+/?$",
]

# Always discard
EXCLUDE_PATTERNS = [
    r"/\d{4}/",        # date-based article paths
    r"/news/",
    r"/blog/",
    r"/press/",
    r"/about",
    r"/careers",
    r"/legal",
    r"/privacy",
    r"/terms",
    r"\?",             # query strings
    r"#",              # fragment-only
]

MAX_URLS = 20

NOTION_DB_ID         = "2729baa265ab451f89d03bf6e82162e4"
NOTION_LOG_PAGE_ID   = "37aa8367-d4b2-81dc-bce8-d0035658c0c6"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch(url: str, timeout: int = 15) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning(f"fetch failed — {url}: {e}")
        return None


def fetch_with_playwright(url: str) -> str | None:
    """Render page with a headless browser and return HTML. Used when requests is blocked."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.warning(f"Playwright fallback failed — {url}: {e}")
        return None


def crawl_nav_with_clicks(base_url: str) -> list[str]:
    """
    Use Playwright to render the homepage, click each top-level nav item
    to reveal dropdowns, and collect all links that appear.
    Returns absolute URLs.
    """
    try:
        from playwright.sync_api import sync_playwright
        base_netloc = urlparse(base_url).netloc
        collected: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            page.goto(base_url, timeout=30_000, wait_until="domcontentloaded")

            # Collect links visible before any clicks
            def harvest_links():
                for a in page.query_selector_all("nav a[href], header a[href]"):
                    try:
                        href = a.get_attribute("href") or ""
                        parsed = urlparse(href)
                        if parsed.netloc and parsed.netloc != base_netloc:
                            continue
                        path = parsed.path or "/"
                        collected.add(base_url.rstrip("/") + "/" + path.lstrip("/"))
                    except Exception:
                        pass

            harvest_links()

            # Click each top-level nav item and harvest newly revealed links
            nav_items = page.query_selector_all("nav > ul > li, nav > div > ul > li, header nav li")
            log.info(f"Found {len(nav_items)} top-level nav items to click")

            for item in nav_items:
                try:
                    item.hover()
                    page.wait_for_timeout(300)
                    harvest_links()
                except Exception:
                    pass

            browser.close()

        log.info(f"Playwright click-nav yielded {len(collected)} raw links")
        return list(collected)

    except Exception as e:
        log.warning(f"Playwright click-nav failed — {base_url}: {e}")
        return []

# ---------------------------------------------------------------------------
# URL classification & prioritization
# ---------------------------------------------------------------------------

def is_excluded(path: str) -> bool:
    return any(re.search(p, path) for p in EXCLUDE_PATTERNS)


def path_depth(path: str) -> int:
    return len([s for s in path.split("/") if s])


def classify_path(path: str) -> str | None:
    """
    Returns priority tier or None (discard).
    Tiers: 'homepage' > 'product' > 'subpage' > 'best'
    """
    clean = path.rstrip("/") or "/"
    if clean == "/":
        return "homepage"
    if is_excluded(clean):
        return None
    if path_depth(clean) > 3:
        return None
    # Exact product root: /credit-cards/, /personal-loans/, etc.
    first_segment = clean.lstrip("/").split("/")[0]
    if first_segment in PRODUCT_ROOTS and path_depth(clean) == 1:
        return "product"
    # One level under a product root: /credit-cards/rewards/, etc.
    if PRODUCT_SUBPAGE_PATTERN.match(clean):
        return "subpage"
    # Best-of pages: /best-credit-cards/ or /credit-cards/best-rewards/
    if any(re.match(p, clean) for p in BEST_OF_PATTERNS):
        return "best"
    return None


def prioritize_and_trim(classified: list[dict], trimmed_log: list[str]) -> list[str]:
    """Sort by priority tier, trim to MAX_URLS, record discards."""
    tier_order = {"homepage": 0, "product": 1, "subpage": 2, "best": 3}
    classified.sort(key=lambda x: tier_order.get(x["tier"], 99))
    kept      = classified[:MAX_URLS]
    discarded = classified[MAX_URLS:]
    for item in discarded:
        trimmed_log.append(item["url"])
    return [item["url"] for item in kept]

# ---------------------------------------------------------------------------
# Sitemap crawler
# ---------------------------------------------------------------------------

def crawl_sitemap(base_url: str) -> list[str]:
    """Return all absolute URLs found in sitemap(s). Empty list if unavailable."""
    found = []
    for path in ["/sitemap_index.xml", "/sitemap.xml"]:
        r = fetch(base_url + path)
        if not r:
            continue
        soup = BeautifulSoup(r.content, "xml")

        # Sitemap index — recurse into child sitemaps
        child_sitemaps = soup.find_all("sitemap")
        if child_sitemaps:
            for s in child_sitemaps:
                loc = s.find("loc")
                if not loc:
                    continue
                child_r = fetch(loc.text.strip())
                if child_r:
                    child_soup = BeautifulSoup(child_r.content, "xml")
                    for url_tag in child_soup.find_all("url"):
                        loc_tag = url_tag.find("loc")
                        if loc_tag:
                            found.append(loc_tag.text.strip())

        # Regular sitemap
        for url_tag in soup.find_all("url"):
            loc_tag = url_tag.find("loc")
            if loc_tag:
                found.append(loc_tag.text.strip())

        if found:
            log.info(f"Sitemap at {path} yielded {len(found)} raw URLs")
            break

    return found

# ---------------------------------------------------------------------------
# Nav crawl fallback
# ---------------------------------------------------------------------------

def crawl_nav(base_url: str, html: str | None = None) -> list[str]:
    """Extract href links from homepage <nav> / <header> elements."""
    if html is None:
        r = fetch(base_url)
        html = r.text if r else fetch_with_playwright(base_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc
    paths = []

    for container in soup.find_all(["nav", "header"]):
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            parsed = urlparse(href)
            # Keep same-domain or relative links only
            if parsed.netloc and parsed.netloc != base_netloc:
                continue
            path = parsed.path or "/"
            paths.append(base_url.rstrip("/") + "/" + path.lstrip("/"))

    log.info(f"Nav crawl yielded {len(paths)} raw links")
    return paths

# ---------------------------------------------------------------------------
# Core discovery
# ---------------------------------------------------------------------------

def discover(competitor_key: str) -> tuple[list[str], str, list[str]]:
    """
    Returns:
        url_list    — final prioritized URL list
        method      — 'sitemap' | 'nav' | 'playwright_nav'
        trimmed     — URLs that were cut to stay within MAX_URLS
    """
    base_url   = COMPETITORS[competitor_key]
    base_netloc = urlparse(base_url).netloc
    trimmed: list[str] = []

    # --- Attempt 1: sitemap ---
    raw_urls = crawl_sitemap(base_url)
    method   = "sitemap"

    # --- Attempt 2: static nav crawl ---
    if not raw_urls:
        log.info(f"{competitor_key}: sitemap empty or unavailable, trying nav crawl")
        raw_urls = crawl_nav(base_url)
        method   = "nav"

    # --- Attempt 3: Playwright static render ---
    if not raw_urls:
        log.info(f"{competitor_key}: nav crawl empty, trying Playwright static render")
        html     = fetch_with_playwright(base_url)
        raw_urls = crawl_nav(base_url, html=html)
        method   = "playwright_nav"

    # --- Attempt 4: Playwright with nav clicks ---
    # Always run if we have fewer than 5 URLs — JS dropdowns likely hiding more
    if len(raw_urls) < 5:
        log.info(f"{competitor_key}: fewer than 5 URLs found ({len(raw_urls)}), trying Playwright click-nav")
        click_urls = crawl_nav_with_clicks(base_url)
        if click_urls:
            raw_urls = list(set(raw_urls) | set(click_urls))
            method   = "playwright_click_nav"
            log.info(f"{competitor_key}: click-nav added URLs, total raw: {len(raw_urls)}")

    # --- Classify ---
    classified: list[dict] = []
    seen: set[str] = set()

    for url in raw_urls:
        parsed = urlparse(url)
        # Drop external domains
        if parsed.netloc and parsed.netloc != base_netloc:
            continue
        path = parsed.path or "/"
        full_url = base_url.rstrip("/") + "/" + path.lstrip("/")
        if full_url in seen:
            continue
        tier = classify_path(path)
        if tier:
            seen.add(full_url)
            classified.append({"url": full_url, "tier": tier})

    # Always ensure homepage is present
    if not any(c["tier"] == "homepage" for c in classified):
        classified.insert(0, {"url": base_url, "tier": "homepage"})

    url_list = prioritize_and_trim(classified, trimmed)
    return url_list, method, trimmed

# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def get_competitor_page_id(notion: Client, competitor_key: str) -> str | None:
    display_name = COMPETITOR_DISPLAY_NAMES[competitor_key]
    results = notion.databases.query(
        **{"database_id": NOTION_DB_ID,
           "filter": {"property": "Competitor", "title": {"contains": display_name}}}
    )
    pages = results.get("results", [])
    if not pages:
        log.error(f"No Notion page found for: {display_name}")
        return None
    return pages[0]["id"]


def update_competitor_profile(notion: Client, page_id: str, urls: list[str]) -> None:
    url_text = "\n".join(urls)
    notion.pages.update(
        page_id=page_id,
        properties={
            "Monitored URLs":      {"rich_text": [{"text": {"content": url_text}}]},
            "Last Discovery Run":  {"date": {"start": datetime.now(timezone.utc).date().isoformat()}},
            "Discovery Status":    {"select": {"name": "Current"}},
            "Consecutive 404s":    {"number": 0},
            "Nav Change Detected": {"checkbox": False},
            "Drift Signal Count":  {"number": 0},
        },
    )
    log.info(f"Notion updated — {len(urls)} URLs written")


def set_competitor_stale(notion: Client, page_id: str) -> None:
    notion.pages.update(
        page_id=page_id,
        properties={"Discovery Status": {"select": {"name": "Stale"}}},
    )


def append_run_log(
    notion: Client,
    competitor_key: str,
    urls: list[str],
    method: str,
    trimmed: list[str],
    error: str | None = None,
) -> None:
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = "Error" if error else "Complete"
    notes_lines = [f"Method: {method}"]
    if error:
        notes_lines.append(f"Error: {error}")
    else:
        notes_lines.append(f"URLs written: {len(urls)}")
        if trimmed:
            trimmed_str = f"Trimmed (exceeded {MAX_URLS}): {', '.join(trimmed)}"
            # Truncate trimmed list if it would push entry over Notion's 2000 char limit
            if len(trimmed_str) > 300:
                trimmed_str = f"Trimmed (exceeded {MAX_URLS}): {len(trimmed)} URLs discarded"
            notes_lines.append(trimmed_str)

    entry = (
        f"---\n"
        f"Date: {now}\n"
        f"Agent: Discovery Agent\n"
        f"Competitor: {COMPETITOR_DISPLAY_NAMES[competitor_key]}\n"
        f"Status: {status}\n"
        f"Notes: {' | '.join(notes_lines)}\n"
        f"---"
    )

    # Hard cap at 1900 chars to stay safely under Notion's 2000 limit
    if len(entry) > 1900:
        entry = entry[:1897] + "..."

    notion.blocks.children.append(
        block_id=NOTION_LOG_PAGE_ID,
        children=[{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": entry}}]
            },
        }],
    )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Finance Discovery Agent")
    parser.add_argument(
        "competitor",
        choices=list(COMPETITORS.keys()),
        help="Competitor key to run discovery for",
    )
    args = parser.parse_args()
    competitor_key = args.competitor

    notion = Client(auth=os.environ["NOTION_TOKEN"])
    log.info(f"Starting discovery: {COMPETITOR_DISPLAY_NAMES[competitor_key]}")

    try:
        urls, method, trimmed = discover(competitor_key)

        page_id = get_competitor_page_id(notion, competitor_key)
        if not page_id:
            sys.exit(1)

        if not urls:
            msg = f"No URLs discovered via {method} — competitor may be blocking crawls"
            log.error(msg)
            set_competitor_stale(notion, page_id)
            append_run_log(notion, competitor_key, [], method, [], error=msg)
            sys.exit(1)

        log.info(f"Discovered {len(urls)} URLs via {method}:")
        for u in urls:
            log.info(f"  {u}")

        update_competitor_profile(notion, page_id, urls)
        append_run_log(notion, competitor_key, urls, method, trimmed)
        log.info(f"Discovery complete: {COMPETITOR_DISPLAY_NAMES[competitor_key]}")

    except Exception as e:
        log.exception(f"Unexpected error for {competitor_key}: {e}")
        page_id = get_competitor_page_id(notion, competitor_key)
        if page_id:
            set_competitor_stale(notion, page_id)
        append_run_log(notion, competitor_key, [], "unknown", [], error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
