"""
NAFDAC Greenbook scraper.

Scrapes product detail pages from http://greenbook.nafdac.gov.ng/products/details/{id}
and persists results to a local SQLite database (drugs.db).

Usage:
    python scraper.py                  # scrape IDs 1–15000
    python scraper.py --start 1 --end 500
    python scraper.py --ids 42 100 200
"""

import asyncio
import logging
import sqlite3
import argparse
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://greenbook.nafdac.gov.ng")
DB_PATH = os.getenv("DB_PATH", "drugs.db")
CONCURRENCY = 5
BATCH_SIZE = 25
BATCH_DELAY = 0.5  # seconds between batches
REQUEST_TIMEOUT = 20.0

logging.basicConfig(
    filename="scrape_errors.log",
    level=logging.ERROR,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS drugs (
    id                  INTEGER PRIMARY KEY,
    product_name        TEXT,
    active_ingredients  TEXT,
    strength            TEXT,
    dosage_form         TEXT,
    roa                 TEXT,
    applicant_name      TEXT,
    nrn                 TEXT UNIQUE,
    status              TEXT,
    atc_code            TEXT,
    product_category    TEXT,
    marketing_category  TEXT,
    pack_size           TEXT,
    product_description TEXT,
    manufacturer_name   TEXT,
    manufacturer_country TEXT,
    approval_date       TEXT,
    expiry_date         TEXT,
    scraped_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_nrn      ON drugs(nrn);
CREATE INDEX IF NOT EXISTS idx_status   ON drugs(status);
CREATE INDEX IF NOT EXISTS idx_category ON drugs(product_category);
CREATE INDEX IF NOT EXISTS idx_country  ON drugs(manufacturer_country);
CREATE INDEX IF NOT EXISTS idx_mktcat   ON drugs(marketing_category);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(DDL)
    conn.commit()
    return conn


def get_scraped_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT id FROM drugs").fetchall()
    return {r[0] for r in rows}


def get_null_ingredient_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM drugs WHERE active_ingredients IS NULL ORDER BY id"
    ).fetchall()
    return [r[0] for r in rows]


def count_null_ingredients(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM drugs WHERE active_ingredients IS NULL"
    ).fetchone()[0]


def upsert_drug(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO drugs (
            id, product_name, active_ingredients, strength, dosage_form,
            roa, applicant_name, nrn, status, atc_code, product_category,
            marketing_category, pack_size, product_description,
            manufacturer_name, manufacturer_country, approval_date,
            expiry_date, scraped_at
        ) VALUES (
            :id, :product_name, :active_ingredients, :strength, :dosage_form,
            :roa, :applicant_name, :nrn, :status, :atc_code, :product_category,
            :marketing_category, :pack_size, :product_description,
            :manufacturer_name, :manufacturer_country, :approval_date,
            :expiry_date, :scraped_at
        )
        """,
        record,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

# Maps <h1> label text (lowercased) to the corresponding DB column.
# The first <h1> on the page is always the product name; subsequent <h1>s
# are field labels, each followed immediately by a <p> with the value.
H1_FIELD_MAP: dict[str, str] = {
    "roa":                          "roa",
    "route of administration":      "roa",
    "applicant name":               "applicant_name",
    "applicant":                    "applicant_name",
    "nrn":                          "nrn",
    "nafdac registration number":   "nrn",
    "registration number":          "nrn",
    "status":                       "status",
    "atc code":                     "atc_code",
    "atc code/atcvet code":         "atc_code",   # actual label on live pages
    "atcvet code":                  "atc_code",
    "product category":             "product_category",
    "category":                     "product_category",
    "marketing category":           "marketing_category",
    "pack size":                    "pack_size",
    "packsize":                     "pack_size",   # actual label on live pages
    "product description":          "product_description",
    "composition":                  "product_description",   # actual label on live pages
    "description":                  "product_description",
    "manufacturer name":            "manufacturer_name",
    "manufacturer":                 "manufacturer_name",
    "manufacturer country":         "manufacturer_country",
    "country":                      "manufacturer_country",
    "approval date":                "approval_date",
    "expiry date":                  "expiry_date",
    "date of expiry":               "expiry_date",
}


def clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


# ---------------------------------------------------------------------------
# Regex helpers for combined-field fallback parsing
# ---------------------------------------------------------------------------

# Unit alternation — compound units MUST appear before their substrings
# so "mg/mL" beats "mg" in the alternation.
_DOSE_UNIT = (
    r'(?:mg/ml|mg/g|mg/5\s?ml|mcg/ml|g/ml|iu/ml'
    r'|mg|g|mcg|µg|ug|ml|mL|IU|iu|mmol|mEq|%|units?)'
)
# One dose value: "25 mg" or "400 mg/250 mL" (N unit / N unit)
_SINGLE_DOSE = r'\d+(?:[.,]\d+)?\s*' + _DOSE_UNIT + r'(?:\s*/\s*\d+(?:[.,]\d+)?\s*' + _DOSE_UNIT + r')?'
# Full strength: one dose, optionally followed by "; N unit …" for combinations
_STRENGTH_RE = re.compile(
    r'\b' + _SINGLE_DOSE + r'(?:\s*;\s*' + _SINGLE_DOSE + r')*',
    re.IGNORECASE,
)

# Matches dosage-form keywords; longer/more-specific alternatives are listed first
# so finditer naturally prefers them.
_DOSAGE_FORM_RE = re.compile(
    r'\b(?:oral\s+solution|oral\s+suspension'
    r'|film[- ]coated\s+tablets?|coated\s+tablets?'
    r'|solution\s+for\s+(?:injection|infusion|inhalation)'
    r'|hard\s+capsules?|soft\s+(?:gelatin\s+)?capsules?'
    r'|eye\s+drops?|ear\s+drops?|nasal\s+(?:spray|drops?)'
    r'|tablets?|capsules?|syrups?|injections?|infusions?'
    r'|creams?|ointments?|suspensions?|solutions?'
    r'|drops?|powders?|suppositories?|gels?|lotions?'
    r'|aerosols?|sprays?|patches?|granules?|pellets?'
    r'|emulsions?|elixirs?|pessaries?|pastilles?|lozenges?'
    r'|sachets?|enemas?|implants?)\b',
    re.IGNORECASE,
)

# Trailing pharmacopoeia codes to strip when extracting ingredient from composition
_PHARMA_CODE_RE = re.compile(
    r'\s+(?:BP|USP|IP|BP/USP|Ph\.Eur\.?|NF|EP|JP)\s*$',
    re.IGNORECASE,
)


def _split_combined(text: str) -> tuple[str | None, str | None, str | None]:
    """Split a collapsed intro span into (active_ingredients, strength, dosage_form).

    Handles:
      "Spironolactone 25 mg Tablet"
      "Telmisartan; Hydrochlorothiazide 40 mg; 12.5 mg Tablet"
    """
    ingredient = strength = dosage_form = None

    sm = _STRENGTH_RE.search(text)
    if sm:
        strength = sm.group().strip()
        ingredient = clean(text[: sm.start()])

    # Use the *last* dosage-form match so multi-word forms ("Solution for injection")
    # beat partial earlier hits ("Solution").
    fm = None
    for m in _DOSAGE_FORM_RE.finditer(text):
        fm = m
    if fm:
        dosage_form = fm.group().strip()
        if not ingredient:
            ingredient = clean(text[: fm.start()])

    return ingredient or None, strength or None, dosage_form or None


def _extract_from_composition(text: str) -> tuple[str | None, str | None]:
    """Extract (active_ingredient, strength) from a Composition/Product Description string.

    e.g.  "Each film coated tablet contains: Spironolactone BP 25 mg Excipients q.s."
          → ("Spironolactone BP", "25 mg")
          "Each mL contains: Pentazocine 30 mg/mL"
          → ("Pentazocine", "30 mg/mL")
    """
    # Drop everything up to and including "contains:" (handles both
    # "Each tablet contains:" and "Acyclovir Ointment contains:")
    body = re.sub(r'^.*?\bcontains\s*:\s*', '', text, flags=re.IGNORECASE).strip()

    sm = _STRENGTH_RE.search(body)
    if not sm:
        return None, None

    strength = sm.group().strip()
    before = body[: sm.start()].strip()
    ingredient = _PHARMA_CODE_RE.sub("", before).strip()

    return ingredient or None, strength or None


# ---------------------------------------------------------------------------
# Main page parser
# ---------------------------------------------------------------------------

def _next_p_before_h1(h1_tag) -> str | None:
    """Return the text of the first <p> after h1_tag, stopping at the next <h1>."""
    for tag in h1_tag.find_all_next():
        if tag.name == "h1":
            break
        if tag.name == "p":
            return clean(tag.get_text())
    return None


def parse_product_page(html: str, greenbook_id: int) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    h1s = soup.find_all("h1")
    if not h1s:
        return None

    record: dict = {
        "id": greenbook_id,
        "product_name": None,
        "active_ingredients": None,
        "strength": None,
        "dosage_form": None,
        "roa": None,
        "applicant_name": None,
        "nrn": None,
        "status": None,
        "atc_code": None,
        "product_category": None,
        "marketing_category": None,
        "pack_size": None,
        "product_description": None,
        "manufacturer_name": None,
        "manufacturer_country": None,
        "approval_date": None,
        "expiry_date": None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    # First <h1> = product name
    record["product_name"] = clean(h1s[0].get_text())

    # <span> tags between first and second <h1> = ingredients, strength, dosage form.
    # Normal layout: 3 separate spans.  Edge case: one collapsed span.
    intro_spans: list[str] = []
    for tag in h1s[0].find_all_next():
        if tag.name == "h1":
            break
        if tag.name == "span":
            t = clean(tag.get_text())
            if t:
                intro_spans.append(t)

    if len(intro_spans) >= 3:
        record["active_ingredients"] = intro_spans[0]
        record["strength"]           = intro_spans[1]
        record["dosage_form"]        = intro_spans[2]
    elif len(intro_spans) == 2:
        record["active_ingredients"] = intro_spans[0]
        record["strength"]           = intro_spans[1]
    elif len(intro_spans) == 1:
        # Single combined span — try regex split
        ing, str_, frm = _split_combined(intro_spans[0])
        record["active_ingredients"] = ing
        record["strength"]           = str_
        record["dosage_form"]        = frm

    # Remaining <h1>s are field labels; the next <p> holds the value
    for h1 in h1s[1:]:
        label = clean(h1.get_text())
        if not label:
            continue
        col = H1_FIELD_MAP.get(label.lower())
        if not col:
            continue
        val = _next_p_before_h1(h1)
        if val:
            record[col] = val

    # Composition fallback: extract ingredient + strength from the composition text
    # when the span-based approach left them null.
    if record["product_description"] and (
        record["active_ingredients"] is None or record["strength"] is None
    ):
        ing, str_ = _extract_from_composition(record["product_description"])
        if ing and record["active_ingredients"] is None:
            record["active_ingredients"] = ing
        if str_ and record["strength"] is None:
            record["strength"] = str_

    if not record["product_name"]:
        return None

    return record


# ---------------------------------------------------------------------------
# Async fetcher
# ---------------------------------------------------------------------------

async def fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    greenbook_id: int,
    pbar: tqdm,
) -> dict | None:
    url = f"{BASE_URL}/products/details/{greenbook_id}"
    async with sem:
        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                pbar.update(1)
                return None
            resp.raise_for_status()
            record = parse_product_page(resp.text, greenbook_id)
            pbar.update(1)
            return record
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s for id=%d  url=%s", exc.response.status_code, greenbook_id, url)
        except httpx.RequestError as exc:
            logger.error("Request error for id=%d: %s", greenbook_id, exc)
        except Exception as exc:
            logger.error("Unexpected error for id=%d: %s", greenbook_id, exc)
        pbar.update(1)
        return None


async def scrape(ids_to_fetch: list[int], conn: sqlite3.Connection) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; NAFDACBot/1.0; +https://github.com/nafdac-api)"
        )
    }

    total = len(ids_to_fetch)
    saved = 0
    skipped = 0

    with tqdm(total=total, desc="Scraping", unit="product") as pbar:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            for batch_start in range(0, total, BATCH_SIZE):
                batch = ids_to_fetch[batch_start : batch_start + BATCH_SIZE]
                tasks = [
                    fetch_one(client, sem, gid, pbar) for gid in batch
                ]
                results = await asyncio.gather(*tasks)

                for record in results:
                    if record is None:
                        skipped += 1
                        continue
                    try:
                        upsert_drug(conn, record)
                        saved += 1
                    except sqlite3.IntegrityError as exc:
                        logger.error("DB integrity error for nrn=%s: %s", record.get("nrn"), exc)
                        skipped += 1

                if batch_start + BATCH_SIZE < total:
                    await asyncio.sleep(BATCH_DELAY)

    print(f"\nDone. Saved: {saved}  |  Skipped/empty: {skipped}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NAFDAC Greenbook scraper")
    parser.add_argument("--start",      type=int, default=1,     help="First ID to scrape (default: 1)")
    parser.add_argument("--end",        type=int, default=15000, help="Last ID to scrape (default: 15000)")
    parser.add_argument("--ids",        type=int, nargs="+",     help="Explicit list of IDs to scrape")
    parser.add_argument("--db",         type=str, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--force",      action="store_true",     help="Re-scrape IDs already in the database")
    parser.add_argument("--fix-nulls",  action="store_true",     help="Re-scrape only records where active_ingredients IS NULL")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    conn = init_db(args.db)

    if args.fix_nulls:
        ids_to_fetch = get_null_ingredient_ids(conn)
        if not ids_to_fetch:
            print("No records with null active_ingredients — nothing to fix.")
            conn.close()
            return
        nulls_before = len(ids_to_fetch)
        print(f"Re-scraping {nulls_before} records where active_ingredients IS NULL…")
        asyncio.run(scrape(ids_to_fetch, conn))
        nulls_after = count_null_ingredients(conn)
        fixed = nulls_before - nulls_after
        print(f"Fixed: {fixed} / {nulls_before}  ({nulls_after} still null after re-scrape)")
        conn.close()
        return

    already_done = set() if args.force else get_scraped_ids(conn)

    if args.ids:
        candidates = args.ids
    else:
        candidates = list(range(args.start, args.end + 1))

    ids_to_fetch = [i for i in candidates if i not in already_done]

    if not ids_to_fetch:
        print("Nothing to scrape — all requested IDs are already in the database.")
        print("Use --force to re-scrape existing records.")
        conn.close()
        return

    print(f"IDs to scrape: {len(ids_to_fetch)}  (skipping {len(already_done)} already done)")
    asyncio.run(scrape(ids_to_fetch, conn))
    conn.close()


if __name__ == "__main__":
    main()
