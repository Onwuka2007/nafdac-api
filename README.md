# NAFDAC Greenbook API

A REST API that scrapes the [NAFDAC Greenbook](http://greenbook.nafdac.gov.ng) and serves Nigerian drug product data.

---

## Setup

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure (optional)

Copy `.env` and edit as needed:

```bash
cp .env .env.local
```

| Variable   | Default                           | Description                  |
|------------|-----------------------------------|------------------------------|
| `BASE_URL` | `http://greenbook.nafdac.gov.ng`  | Greenbook base URL           |
| `DB_PATH`  | `drugs.db`                        | SQLite database file path    |
| `PORT`     | `8000`                            | API server port              |

---

## Part 1 — Scraper

Scrape IDs 1 to 15 000 (resumable; skips already-saved IDs automatically):

```bash
python scraper.py
```

### Options

```
--start INT    First ID to scrape (default: 1)
--end   INT    Last ID to scrape  (default: 15000)
--ids   INT…   Explicit list of IDs, e.g. --ids 42 100 500
--db    PATH   Override DB path
--force        Re-scrape IDs already in the database
```

### Examples

```bash
# Scrape a small test range first
python scraper.py --start 1 --end 100

# Resume after interruption (safe to re-run)
python scraper.py

# Re-scrape specific IDs
python scraper.py --ids 42 1337 9999 --force

# Use a custom database
python scraper.py --db /data/nafdac.db
```

Errors are logged to `scrape_errors.log`. Progress is shown with a live progress bar.

---

## Part 2 — REST API

Start the server:

```bash
uvicorn api:app --reload --port 8000
```

Interactive docs available at:  
- **Swagger UI**: http://localhost:8000/docs  
- **ReDoc**: http://localhost:8000/redoc

---

## API Reference

All list endpoints return an openFDA-style envelope:

```json
{
  "meta": { "total": 14321, "page": 1, "limit": 20, "pages": 717 },
  "results": [ { ... } ]
}
```

### `GET /drugs`

List products with optional filtering.

**Query parameters**

| Param                  | Type   | Default | Description                        |
|------------------------|--------|---------|------------------------------------|
| `page`                 | int    | 1       | Page number                        |
| `limit`                | int    | 20      | Results per page (max 100)         |
| `category`             | string | —       | Filter by `product_category`       |
| `status`               | string | —       | `Active` or `Expired`              |
| `manufacturer_country` | string | —       | Filter by manufacturer country     |
| `marketing_category`   | string | —       | `OTC` or `Prescription`           |

```bash
# First 20 active OTC drugs
curl "http://localhost:8000/drugs?status=Active&marketing_category=OTC"

# Nigerian manufacturers, page 3
curl "http://localhost:8000/drugs?manufacturer_country=Nigeria&page=3&limit=50"
```

---

### `GET /drugs/{nafdac_number}`

Retrieve a single product by its NAFDAC Registration Number.

```bash
curl "http://localhost:8000/drugs/04-0264"
```

```json
{
  "id": 1042,
  "product_name": "PARACETAMOL TABLETS BP",
  "active_ingredients": "Paracetamol",
  "strength": "500mg",
  "dosage_form": "Tablet",
  "roa": "Oral",
  "applicant_name": "ACME Pharma Ltd",
  "nrn": "04-0264",
  "status": "Active",
  "atc_code": "N02BE01",
  "product_category": "Drugs",
  "marketing_category": "OTC",
  "pack_size": "1000 tablets",
  "product_description": "Each tablet contains Paracetamol 500mg",
  "manufacturer_name": "ACME Manufacturing",
  "manufacturer_country": "Nigeria",
  "approval_date": "2018-03-15",
  "expiry_date": "2028-03-15",
  "scraped_at": "2026-04-26T00:00:00+00:00"
}
```

---

### `GET /drugs/search?q={query}`

Full-text search across product name, active ingredients, and applicant name.

```bash
curl "http://localhost:8000/drugs/search?q=amoxicillin&limit=5"
curl "http://localhost:8000/drugs/search?q=paracetamol&page=2"
```

---

### `GET /ingredients`

List all unique active ingredients in the database.

```bash
curl "http://localhost:8000/ingredients"
```

```json
{
  "total": 1203,
  "results": ["Acetylsalicylic Acid", "Amoxicillin", "Paracetamol", "..."]
}
```

---

### `GET /manufacturers`

All manufacturers with their product counts, sorted descending.

```bash
curl "http://localhost:8000/manufacturers"
```

```json
{
  "total": 842,
  "results": [
    { "manufacturer_name": "EMZOR PHARMA LTD", "manufacturer_country": "Nigeria", "product_count": 87 },
    { "manufacturer_name": "GSK CONSUMER HEALTHCARE", "manufacturer_country": "UK", "product_count": 64 }
  ]
}
```

---

### `GET /stats`

Summary statistics.

```bash
curl "http://localhost:8000/stats"
```

```json
{
  "total_products": 12540,
  "by_category": { "Drugs": 11200, "Veterinary": 1100, "Cosmetics": 240 },
  "by_status": { "Active": 9800, "Expired": 2740 },
  "by_manufacturer_country": { "Nigeria": 4200, "India": 3100, "China": 1800 },
  "by_marketing_category": { "Prescription": 7400, "OTC": 5140 }
}
```

---

## Notes

- The scraper is polite: max 5 concurrent requests, 0.5 s delay between batches of 25.
- Re-running the scraper is safe — it skips IDs already in `drugs.db`.
- Use `--force` to refresh stale records.
- CORS is open (`*`) — restrict in production as needed.
