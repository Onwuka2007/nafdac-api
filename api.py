"""
NAFDAC Greenbook REST API.

Run:
    uvicorn api:app --reload --port 8000
"""

import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "drugs.db")
PORT = int(os.getenv("PORT", "8000"))


# ---------------------------------------------------------------------------
# Database connection (per-request, thread-safe row_factory)
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify DB exists on startup
    if not os.path.exists(DB_PATH):
        print(
            f"WARNING: Database '{DB_PATH}' not found. "
            "Run scraper.py first to populate it."
        )
    yield


app = FastAPI(
    title="NAFDAC Greenbook API",
    description="Nigerian drug product data scraped from the NAFDAC Greenbook.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clamp_limit(limit: int) -> int:
    return min(max(limit, 1), 100)


def paginate(conn: sqlite3.Connection, sql: str, params: list, page: int, limit: int) -> dict:
    """Run a COUNT and a paginated SELECT, return openFDA-style envelope."""
    limit = clamp_limit(limit)
    offset = (page - 1) * limit

    count_sql = f"SELECT COUNT(*) FROM ({sql})"
    total: int = conn.execute(count_sql, params).fetchone()[0]

    paginated_sql = f"{sql} LIMIT ? OFFSET ?"
    rows = conn.execute(paginated_sql, params + [limit, offset]).fetchall()
    results = [dict(r) for r in rows]

    return {
        "meta": {
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, -(-total // limit)),  # ceiling division
        },
        "results": results,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/drugs", summary="List drug products (paginated + filtered)")
def list_drugs(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: Optional[str] = Query(None, description="Filter by product_category"),
    status: Optional[str] = Query(None, description="Filter by status (Active/Expired)"),
    manufacturer_country: Optional[str] = Query(None),
    marketing_category: Optional[str] = Query(None, description="OTC or Prescription"),
    db: sqlite3.Connection = Depends(get_db),
):
    sql = "SELECT * FROM drugs WHERE 1=1"
    params: list = []

    if category:
        sql += " AND LOWER(product_category) = LOWER(?)"
        params.append(category)
    if status:
        sql += " AND LOWER(status) = LOWER(?)"
        params.append(status)
    if manufacturer_country:
        sql += " AND LOWER(manufacturer_country) = LOWER(?)"
        params.append(manufacturer_country)
    if marketing_category:
        sql += " AND LOWER(marketing_category) = LOWER(?)"
        params.append(marketing_category)

    sql += " ORDER BY id"
    return paginate(db, sql, params, page, limit)


@app.get("/drugs/search", summary="Full-text search across name, ingredients, applicant")
def search_drugs(
    q: str = Query(..., min_length=2, description="Search query"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
):
    like = f"%{q}%"
    sql = """
        SELECT * FROM drugs
        WHERE product_name        LIKE ?
           OR active_ingredients  LIKE ?
           OR applicant_name      LIKE ?
        ORDER BY id
    """
    params = [like, like, like]
    return paginate(db, sql, params, page, limit)


@app.get("/drugs/{nafdac_number}", summary="Look up a drug by NAFDAC Registration Number")
def get_drug(nafdac_number: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute(
        "SELECT * FROM drugs WHERE nrn = ?", (nafdac_number,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Drug with NRN '{nafdac_number}' not found.")
    return dict(row)


@app.get("/ingredients", summary="List all unique active ingredients")
def list_ingredients(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        "SELECT DISTINCT active_ingredients FROM drugs "
        "WHERE active_ingredients IS NOT NULL "
        "ORDER BY active_ingredients"
    ).fetchall()
    ingredients = [r["active_ingredients"] for r in rows]
    return {"total": len(ingredients), "results": ingredients}


@app.get("/manufacturers", summary="List all unique manufacturers with product count")
def list_manufacturers(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        """
        SELECT manufacturer_name, manufacturer_country,
               COUNT(*) AS product_count
        FROM drugs
        WHERE manufacturer_name IS NOT NULL
        GROUP BY manufacturer_name, manufacturer_country
        ORDER BY product_count DESC
        """
    ).fetchall()
    results = [dict(r) for r in rows]
    return {"total": len(results), "results": results}


@app.get("/stats", summary="Summary statistics for the database")
def stats(db: sqlite3.Connection = Depends(get_db)):
    total = db.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]

    by_category = {
        r["product_category"]: r["cnt"]
        for r in db.execute(
            "SELECT product_category, COUNT(*) cnt FROM drugs "
            "GROUP BY product_category ORDER BY cnt DESC"
        ).fetchall()
    }

    by_status = {
        r["status"]: r["cnt"]
        for r in db.execute(
            "SELECT status, COUNT(*) cnt FROM drugs "
            "GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
    }

    by_country = {
        r["manufacturer_country"]: r["cnt"]
        for r in db.execute(
            "SELECT manufacturer_country, COUNT(*) cnt FROM drugs "
            "WHERE manufacturer_country IS NOT NULL "
            "GROUP BY manufacturer_country ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
    }

    by_marketing = {
        r["marketing_category"]: r["cnt"]
        for r in db.execute(
            "SELECT marketing_category, COUNT(*) cnt FROM drugs "
            "WHERE marketing_category IS NOT NULL "
            "GROUP BY marketing_category ORDER BY cnt DESC"
        ).fetchall()
    }

    return {
        "total_products": total,
        "by_category": by_category,
        "by_status": by_status,
        "by_manufacturer_country": by_country,
        "by_marketing_category": by_marketing,
    }
