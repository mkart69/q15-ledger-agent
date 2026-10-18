import json
import os
import re
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

LEDGER_URL = os.environ.get("LEDGER_URL", "").strip()
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Ledger Agent")
orders = []
rates = {}

def fetch_bytes(url, timeout=90, attempts=5):
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Q15-ledger-agent/3.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Could not fetch ledger data: {type(last_error).__name__}: {last_error}")

def parse_json_or_ndjson(raw):
    text = raw.decode("utf-8-sig").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

def normalize_export(data):
    if isinstance(data, dict):
        for key in ("orders", "data", "results", "records"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    if isinstance(data, list):
        return data
    return []

@app.on_event("startup")
def load_data():
    global orders, rates
    if not LEDGER_URL:
        print("ERROR: LEDGER_URL environment variable is not set")
        return

    metadata = json.loads(fetch_bytes(LEDGER_URL).decode("utf-8-sig"))
    links = metadata.get("links", {})
    export_url = links.get("export")
    rates_url = links.get("rates")
    if not export_url or not rates_url:
        raise RuntimeError("Ledger metadata does not contain export/rates links")

    export_data = parse_json_or_ndjson(fetch_bytes(export_url))
    raw_orders = normalize_export(export_data)

    # The export can contain more than one version of an order. Keep the latest update.
    latest = {}
    for order in raw_orders:
        if not isinstance(order, dict) or not order.get("id"):
            continue
        oid = order["id"]
        if oid not in latest or str(order.get("updated_at", "")) > str(latest[oid].get("updated_at", "")):
            latest[oid] = order
    orders = list(latest.values())

    rates_data = parse_json_or_ndjson(fetch_bytes(rates_url))
    if isinstance(rates_data, list):
        rates_data = rates_data[0] if rates_data and isinstance(rates_data[0], dict) else {}
    rates = rates_data if isinstance(rates_data, dict) else {}
    print(f"Loaded {len(orders)} unique ledger orders; rates={list(rates.get('usd_per_unit', {}).keys())}")

class Question(BaseModel):
    question: str

def order_date(order):
    value = order.get("created_at", "")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BUSINESS_TZ)
        return dt.astimezone(BUSINESS_TZ)
    except Exception:
        return None

def usd_amount(order):
    currency = str(order.get("currency", "USD")).upper()
    per_unit = rates.get("usd_per_unit", {})
    rate = per_unit.get(currency)
    if rate is None:
        # USD records can be used without an explicit USD rate; unknown currencies
        # should not silently be treated as USD.
        if currency == "USD":
            rate = 1
        else:
            raise ValueError(f"Missing USD conversion rate for currency {currency}")
    return float(order.get("amount", 0) or 0) * float(rate)

def parse_filters(question):
    q = question.lower()
    result = {"region": None, "product": None, "customer": None, "month": None, "year": None, "status": None}

    for region in ("central", "east", "north", "south", "west"):
        if re.search(r"\b" + region + r"\b", q):
            result["region"] = region.title()
            break

    # Longest product name first avoids partial matches if names overlap.
    products = sorted({str(o.get("product", "")) for o in orders if o.get("product")}, key=len, reverse=True)
    for product in products:
        if product.lower() in q:
            result["product"] = product
            break

    customer = re.search(r"\bC\d{4}\b", question, re.I)
    if customer:
        result["customer"] = customer.group(0).upper()

    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }
    for name, number in months.items():
        if re.search(r"\b" + name + r"\b", q):
            result["month"] = number
            break
    year = re.search(r"\b(20\d{2})\b", q)
    if year:
        result["year"] = int(year.group(1))

    if re.search(r"\b(refund|refunded|refunds)\b", q):
        result["status"] = "refunded"
    elif re.search(r"\b(void|voided)\b", q):
        result["status"] = "void"
    elif re.search(r"\bpaid\b", q) or any(word in q for word in ("revenue", "sales", "sold", "spend", "spent", "income", "earned")):
        result["status"] = "paid"
    return result

def filtered_orders(question):
    filters = parse_filters(question)
    selected = []
    for order in orders:
        if filters["region"] and order.get("region") != filters["region"]:
            continue
        if filters["product"] and order.get("product") != filters["product"]:
            continue
        if filters["customer"] and str(order.get("customer", "")).upper() != filters["customer"]:
            continue
        dt = order_date(order)
        if filters["month"] or filters["year"]:
            if dt is None:
                continue
            if filters["month"] and dt.month != filters["month"]:
                continue
            if filters["year"] and dt.year != filters["year"]:
                continue
        if filters["status"] and order.get("status") != filters["status"]:
            continue
        selected.append(order)
    return selected

def is_count_question(q):
    return bool(re.search(r"\b(how many|number of|count|distinct|different|unique)\b", q))

def answer_question(question):
    q = question.lower()
    if not orders:
        raise RuntimeError("Ledger data is not loaded; check LEDGER_URL and the service startup logs.")

    selected = filtered_orders(question)
    paid = [o for o in selected if o.get("status") == "paid"]

    # 1. Distinct-customer questions must precede generic "how many" handling.
    asks_customers = bool(re.search(r"\b(customer|customers|buyers|people)\b", q))
    if asks_customers and is_count_question(q):
        return len({str(o.get("customer", "")).upper() for o in selected if o.get("customer")})

    # 2. Product ranking by revenue: return a product name, not a revenue number.
    asks_product_rank = (
        ("product" in q or "top-selling" in q or "top selling" in q or "best-selling" in q or "best selling" in q)
        and bool(re.search(r"\b(top|most|highest|best|greatest|leading|earned|selling|revenue)\b", q))
    )
    revenue_rank = any(term in q for term in (
        "by revenue", "revenue", "earned the most", "most usd", "highest revenue",
        "highest-selling", "highest selling", "top-selling", "top selling",
        "best-selling", "best selling"
    ))
    if asks_product_rank:
        groups = defaultdict(float)
        for order in paid:
            key = str(order.get("product", ""))
            if key:
                groups[key] += usd_amount(order) if revenue_rank else float(order.get("qty", 0) or 0)
        return max(groups, key=groups.get) if groups else "No matching records"

    # 3. Distinct product questions.
    if "product" in q and is_count_question(q):
        return len({str(o.get("product", "")) for o in selected if o.get("product")})

    # 4. Average order value is average of the individual paid orders after currency conversion.
    if re.search(r"\b(average|mean)\b", q) and any(word in q for word in ("order", "value", "amount", "spend", "purchase")):
        if not paid:
            return 0
        return round(sum(usd_amount(o) for o in paid) / len(paid), 2)

    # 5. Counts of orders (not customers) and units.
    if any(term in q for term in ("units sold", "quantity sold", "total quantity", "how many units", "number of units")):
        return sum(float(o.get("qty", 0) or 0) for o in paid)

    if is_count_question(q) and any(word in q for word in ("order", "orders", "placed")):
        return len(selected)

    # 6. Refund/void totals and counts.
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        refunded = [o for o in selected if o.get("status") == "refunded"]
        if is_count_question(q):
            return len(refunded)
        return round(sum(usd_amount(o) for o in refunded), 2)

    if re.search(r"\b(void|voided)\b", q):
        voided = [o for o in selected if o.get("status") == "void"]
        if is_count_question(q):
            return len(voided)
        return round(sum(usd_amount(o) for o in voided), 2)

    # 7. Revenue/sales totals.
    if any(term in q for term in ("revenue", "sales", "sales total", "total sales", "income", "spent", "spend", "paid amount")):
        return round(sum(usd_amount(o) for o in paid), 2)

    # 8. Fallback: product/customer lists or number of matching rows.
    if "customer" in q and any(word in q for word in ("list", "which", "who")):
        return sorted({str(o.get("customer")) for o in selected if o.get("customer")})
    if "product" in q and any(word in q for word in ("list", "which")):
        return sorted({str(o.get("product")) for o in selected if o.get("product")})
    return len(selected)

@app.get("/")
def root():
    return {"status": "ok", "service": "ledger-agent", "orders_loaded": len(orders)}

@app.post("/answer")
def answer(payload: Question):
    try:
        return {"answer": answer_question(payload.question)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
