#!/usr/bin/env python3
"""
Compare Finance Competitive Intelligence — Re-run Trigger
Polls the Competitor Profiles Notion database for any competitor flagged
'Re-run Needed' and runs discovery_agent.py for each one.

Called by the discovery_triggered GitHub Actions workflow every 4 hours.
"""

import os
import subprocess
import sys
import logging
from notion_client import Client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NOTION_DB_ID = "2729baa265ab451f89d03bf6e82162e4"

# Maps Notion page title → discovery_agent.py competitor key
DISPLAY_NAME_TO_KEY = {
    "NerdWallet":   "nerdwallet",
    "Credit Karma": "creditkarma",
    "Bankrate":     "bankrate",
    "FinanceBuzz":  "financebuzz",
    "LendingTree":  "lendingtree",
    "BestMoney":    "bestmoney",
    "Credible":     "credible",
}

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "discovery_agent.py")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    notion = Client(auth=os.environ["NOTION_TOKEN"])

    log.info("Checking Notion for competitors flagged Re-run Needed...")

    results = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "property": "Discovery Status",
            "select": {"equals": "Re-run Needed"},
        },
    )

    pages = results.get("results", [])

    if not pages:
        log.info("No competitors flagged for re-run. Exiting.")
        return

    log.info(f"Found {len(pages)} competitor(s) to re-run.")
    failed = []

    for page in pages:
        try:
            title_parts = page["properties"]["Name"]["title"]
            if not title_parts:
                log.warning(f"Page {page['id']} has no title, skipping.")
                continue
            title = title_parts[0]["text"]["content"]
        except (KeyError, IndexError) as e:
            log.warning(f"Could not parse title for page {page['id']}: {e}")
            continue

        key = DISPLAY_NAME_TO_KEY.get(title)
        if not key:
            log.warning(f"Unknown competitor title '{title}', skipping.")
            continue

        log.info(f"Running discovery for: {title}")
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, key],
            capture_output=False,   # let stdout/stderr flow to Actions log
        )

        if result.returncode != 0:
            log.error(f"Discovery failed for {title} (exit code {result.returncode})")
            failed.append(title)
        else:
            log.info(f"Discovery complete for: {title}")

    if failed:
        log.error(f"The following competitors failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
