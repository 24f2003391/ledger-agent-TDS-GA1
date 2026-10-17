"""Acme Appliances Ledger Agent.

Set LEDGER_API_URL to the private, signed assignment URL for path=/.
The URL is deliberately read from an environment variable, not committed.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_TZ = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")
HTTP = requests.Session()

app = FastAPI(title="Acme Appliances Ledger Agent", version="1.0.0")

class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

class Ledger:
    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.rates: dict[str, Decimal] = {}
        self.loaded = False
        self.load_error: str | None = None

    def load(self) -> None:
        base_url = os.getenv("LEDGER_API_URL", "").strip()
        if not base_url:
            self.load_error = "LEDGER_API_URL environment variable is not configured"
            return
        try:
            root_response = HTTP.get(base_url, timeout=12)
            root_response.raise_for_status()
            root = root_response.json()
            links = root.get("links", {})
            if not links.get("export") or not links.get("rates"):
                raise ValueError("The ledger discovery response must include export and rates links")

            rates_response = HTTP.get(links["rates"], timeout=12)
            rates_response.raise_for_status()
            rate_data = rates_response.json()
            self.rates = {
                str(currency).upper(): Decimal(str(rate))
                for currency, rate in rate_data.get("usd_per_unit", {}).items()
            }
            if "USD" not in self.rates:
                self.rates["USD"] = Decimal("1")

            export_response = HTTP.get(links["export"], timeout=25)
            export_response.raise_for_status()
            # The export endpoint uses NDJSON: one JSON object per non-empty line.
            rows = []
            for line_number, line in enumerate(export_response.text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid NDJSON at line {line_number}") from exc
                if isinstance(row, dict) and row.get("id"):
                    rows.append(row)

            # Duplicate IDs are revisions; retain the row with latest updated_at.
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                key = str(row["id"])
                previous = latest.get(key)
                if previous is None or self._timestamp(row.get("updated_at")) >= self._timestamp(previous.get("updated_at")):
                    latest[key] = row
            self.orders = list(latest.values())
            self.loaded = True
            self.load_error = None
        except Exception as exc:
            self.loaded = False
            self.load_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not value:
            return datetime.min.replace(tzinfo=APP_TZ)
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=APP_TZ)
            return dt
        except ValueError:
            return datetime.min.replace(tzinfo=APP_TZ)

    def date_of(self, row: dict[str, Any]) -> date | None:
        value = row.get("created_at")
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=APP_TZ)
            return dt.astimezone(APP_TZ).date()
        except ValueError:
            return None

    def usd(self, row: dict[str, Any]) -> Decimal:
        amount = Decimal(str(row.get("amount", 0)))
        currency = str(row.get("currency", "USD")).upper()
        rate = self.rates.get(currency)
        if rate is None:
            raise ValueError(f"Missing USD conversion rate for {currency}")
        return amount * rate

ledger = Ledger()

@app.on_event("startup")
def load_ledger() -> None:
    ledger.load()

@app.get("/")
def health() -> dict[str, Any]:
    return {
        "service": "Acme Appliances Ledger Agent",
        "status": "ready" if ledger.loaded else "not_ready",
        "orders_loaded": len(ledger.orders),
        "message": 'POST JSON {"question": "..."} to this URL.',
    }

@app.get("/health")
def health_alias() -> dict[str, Any]:
    return health()

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
REGIONS = {"north", "south", "east", "west", "central"}
STATUS_WORDS = {
    "paid": {"paid"},
    "refund": {"refunded", "refund", "refunded"},
    "refunded": {"refunded", "refund", "refunded"},
    "pending": {"pending"},
    "cancelled": {"cancelled", "canceled"},
}

def money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))

def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

def parse_period(q: str):
    """Return inclusive start, exclusive end dates, or None."""
    ql = q.lower()
    year_match = re.search(r"\b(20\d{2})\b", ql)
    year = int(year_match.group(1)) if year_match else None
    for month_name, month in MONTHS.items():
        if re.search(rf"\b{month_name}\b", ql):
            y = year or datetime.now(APP_TZ).year
            start = date(y, month, 1)
            end = date(y + (month == 12), (month % 12) + 1, 1)
            return start, end
    # Explicit calendar year.
    if year and re.search(r"\b(year|annual|annually|during|in)\b", ql):
        return date(year, 1, 1), date(year + 1, 1, 1)
    # Date range e.g. 2026-03-01 to 2026-03-31.
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", ql)
    if len(dates) >= 2:
        try:
            return date.fromisoformat(dates[0]), date.fromisoformat(dates[1]).fromordinal(date.fromisoformat(dates[1]).toordinal() + 1)
        except ValueError:
            return None
    return None

def parse_region(q: str) -> str | None:
    for region in REGIONS:
        if re.search(rf"\b{region}\b", q.lower()):
            return region.title()
    return None

def filter_rows(rows: list[dict[str, Any]], q: str) -> list[dict[str, Any]]:
    period = parse_period(q)
    region = parse_region(q)
    qn = normalized(q)
    product = next((str(x.get("product", "")) for x in rows
                    if x.get("product") and normalized(str(x["product"])) in qn), None)
    customer_match = re.search(r"\bC\d{4}\b", q, re.I)
    customer = customer_match.group(0).upper() if customer_match else None
    result = []
    for row in rows:
        if region and str(row.get("region", "")).lower() != region.lower():
            continue
        if period:
            d = ledger.date_of(row)
            if d is None or not (period[0] <= d < period[1]):
                continue
        if product and str(row.get("product", "")).lower() != product.lower():
            continue
        if customer and str(row.get("customer", "")).upper() != customer:
            continue
        result.append(row)
    return result

def answer_question(question: str) -> Any:
    if not ledger.loaded:
        raise HTTPException(status_code=503, detail="Ledger data is not loaded. Check LEDGER_API_URL configuration.")
    q = question.strip()
    ql = q.lower()
    rows = filter_rows(ledger.orders, q)

    # If asking about a named customer/product, the filter_rows helper matches exact IDs/names.
    is_refund = any(w in ql for w in ("refund", "refunded", "refunds"))
    is_revenue = any(w in ql for w in ("revenue", "sales", "sold", "turnover", "income"))
    is_count = any(w in ql for w in ("how many", "count", "number of orders", "number of refunds"))
    is_average = any(w in ql for w in ("average", "avg", "mean"))
    is_top = any(w in ql for w in ("top", "most", "highest", "best-selling", "bestselling", "largest"))
    is_product = any(w in ql for w in ("product", "products", "item", "items"))
    is_customer = any(w in ql for w in ("customer", "customers", "client", "clients"))
    is_order_count = "order" in ql or "orders" in ql

    if is_refund:
        refund_rows = [r for r in rows if str(r.get("status", "")).lower() in {"refunded", "refund"}]
        if is_count:
            return len(refund_rows)
        total = sum((ledger.usd(r) for r in refund_rows), Decimal("0"))
        if is_average:
            return money(total / len(refund_rows)) if refund_rows else 0
        return money(total)

    paid_rows = [r for r in rows if str(r.get("status", "")).lower() == "paid"]

    if is_top and is_product:
        grouped: dict[str, Decimal] = defaultdict(Decimal)
        for row in paid_rows:
            grouped[str(row.get("product", "Unknown"))] += ledger.usd(row)
        if any(w in ql for w in ("quantity", "units", "unit")):
            quantities: dict[str, int] = defaultdict(int)
            for row in paid_rows:
                quantities[str(row.get("product", "Unknown"))] += int(row.get("qty", 0))
            return max(quantities, key=quantities.get) if quantities else ""
        return max(grouped, key=grouped.get) if grouped else ""

    if is_top and is_customer:
        grouped_c: dict[str, Decimal] = defaultdict(Decimal)
        for row in paid_rows:
            grouped_c[str(row.get("customer", "Unknown"))] += ledger.usd(row)
        return max(grouped_c, key=grouped_c.get) if grouped_c else ""

    # Respect explicit "paid orders only" wording for counts as well as totals.
    explicit_paid_only = bool(re.search(
        r"\\b(?:paid orders? only|only paid orders?|paid only|paid-only)\\b", ql
    ))
    count_rows = paid_rows if explicit_paid_only else rows

    if is_count and is_order_count:
        return len(count_rows)
    if is_count and is_customer:
        return len({str(r.get("customer")) for r in count_rows if r.get("customer")})
    if is_count and is_product:
        return len({str(r.get("product")) for r in count_rows if r.get("product")})

    if is_average:
        if is_product or is_customer or "order value" in ql or "order amount" in ql:
            amounts = [ledger.usd(r) for r in paid_rows]
            return money(sum(amounts, Decimal("0")) / len(amounts)) if amounts else 0
        if "quantity" in ql or "units" in ql:
            return round(sum(int(r.get("qty", 0)) for r in paid_rows) / len(paid_rows), 2) if paid_rows else 0

    if is_revenue or any(w in ql for w in ("total", "amount", "value")):
        return money(sum((ledger.usd(r) for r in paid_rows), Decimal("0")))

    if is_product:
        return sorted({str(r.get("product")) for r in rows if r.get("product")})
    if is_customer:
        return sorted({str(r.get("customer")) for r in rows if r.get("customer")})

    if is_count:
        return len(rows)

    # Default to revenue for natural-language questions with a time/region scope.
    if parse_period(q) or parse_region(q):
        return money(sum((ledger.usd(r) for r in paid_rows), Decimal("0")))

    raise HTTPException(status_code=422, detail="I couldn't determine the requested metric. Ask about revenue, refunds, orders, products, or customers.")

@app.post("/")
@app.post("/answer")
def answer(request: QuestionRequest) -> dict[str, Any]:
    try:
        return {"answer": answer_question(request.question)}
    except HTTPException:
        raise
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
