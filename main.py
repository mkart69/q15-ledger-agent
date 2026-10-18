import json
import os
import re
import time
import urllib.request
from datetime import datetime
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

LEDGER_URL = os.environ.get("LEDGER_URL", "").strip()
app = FastAPI(title="Ledger Agent")

orders = []
rates = {}

def fetch_bytes(url, timeout=90, attempts=4):
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Q15-ledger-agent"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Could not fetch ledger data: {type(last_error).__name__}")

def parse_json_or_ndjson(raw):
    text = raw.decode("utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

@app.on_event("startup")
def load_data():
    global orders, rates
    if not LEDGER_URL:
        print("WARNING: LEDGER_URL environment variable is not set")
        return
    metadata = json.loads(fetch_bytes(LEDGER_URL).decode("utf-8-sig"))
    links = metadata.get("links", {})
    if not links.get("export") or not links.get("rates"):
        raise RuntimeError("Ledger metadata does not contain export/rates links")
    raw_orders = parse_json_or_ndjson(fetch_bytes(links["export"]))
    if isinstance(raw_orders, dict):
        raw_orders = raw_orders.get("orders", raw_orders.get("data", []))
    latest = {}
    for order in raw_orders:
        oid = order.get("id")
        if not oid:
            continue
        if oid not in latest or order.get("updated_at", "") > latest[oid].get("updated_at", ""):
            latest[oid] = order
    orders = list(latest.values())
    rates = parse_json_or_ndjson(fetch_bytes(links["rates"]))
    if isinstance(rates, list):
        rates = rates[0] if rates else {}
    print(f"Loaded {len(orders)} unique ledger orders")

class Question(BaseModel):
    question: str

def order_date(order):
    value = order.get("created_at", "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def usd_amount(order):
    currency = order.get("currency", "USD")
    per_unit = rates.get("usd_per_unit", {})
    rate = float(per_unit.get(currency, 1))
    return float(order.get("amount", 0)) * rate

def parse_filters(question):
    q = question.lower()
    result = {"region": None, "product": None, "customer": None, "month": None, "year": None, "status": None}
    regions = ["central", "east", "north", "south", "west"]
    for region in regions:
        if re.search(r"\b" + region + r"\b", q):
            result["region"] = region.title()
            break
    for product in sorted({o.get("product", "") for o in orders}, key=len, reverse=True):
        if product and product.lower() in q:
            result["product"] = product
            break
    customer = re.search(r"\bC\d{4}\b", question, re.I)
    if customer:
        result["customer"] = customer.group(0).upper()
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12
    }
    for name, number in months.items():
        if re.search(r"\b" + name + r"\b", q):
            result["month"] = number
            break
    year = re.search(r"\b(20\d{2})\b", q)
    if year:
        result["year"] = int(year.group(1))
    if "refund" in q or "refunded" in q:
        result["status"] = "refunded"
    elif "void" in q or "voided" in q:
        result["status"] = "void"
    elif "paid" in q:
        result["status"] = "paid"
    elif any(word in q for word in ["revenue", "sales", "sold", "spend", "spent", "income"]):
        result["status"] = "paid"
    return result

def filtered_orders(question):
    f = parse_filters(question)
    selected = []
    for o in orders:
        if f["region"] and o.get("region") != f["region"]:
            continue
        if f["product"] and o.get("product") != f["product"]:
            continue
        if f["customer"] and o.get("customer", "").upper() != f["customer"]:
            continue
        d = order_date(o)
        if f["month"] or f["year"]:
            if d is None:
                continue
            if f["month"] and d.month != f["month"]:
                continue
            if f["year"] and d.year != f["year"]:
                continue
        if f["status"] and o.get("status") != f["status"]:
            continue
        selected.append(o)
    return selected, f

def answer_question(question):
    q = question.lower()
    if not orders:
        raise RuntimeError("Ledger data is not loaded; check the LEDGER_URL setting.")
    selected, filters = filtered_orders(question)

    # Grouping/ranking questions
    if any(x in q for x in ["top product", "best-selling product", "best selling product", "most sold product", "which product sold the most", "highest-selling product", "highest selling product"]):
        groups = defaultdict(float)
        for o in selected:
            if o.get("status") == "paid":
                groups[o.get("product", "")] += float(o.get("qty", 0))
        return max(groups, key=groups.get) if groups else "No matching records"

    if any(x in q for x in ["top customer", "highest-spending customer", "highest spending customer", "customer spent the most", "largest customer"]):
        groups = defaultdict(float)
        for o in selected:
            if o.get("status") == "paid":
                groups[o.get("customer", "")] += usd_amount(o)
        return max(groups, key=groups.get) if groups else "No matching records"

    if any(x in q for x in ["how many", "number of orders", "count of orders", "count orders", "how many orders"]):
        return len(selected)

    if any(x in q for x in ["units sold", "quantity sold", "total quantity", "how many units", "number of units"]):
        return sum(float(o.get("qty", 0)) for o in selected if o.get("status") == "paid")

    if any(x in q for x in ["average order", "average value", "mean order"]):
        paid = [o for o in selected if o.get("status") == "paid"]
        return round(sum(usd_amount(o) for o in paid) / len(paid), 2) if paid else 0

    if any(x in q for x in ["refund", "refunded"]):
        total = sum(usd_amount(o) for o in selected if o.get("status") == "refunded")
        if any(x in q for x in ["how many", "count", "number of"]):
            return sum(1 for o in selected if o.get("status") == "refunded")
        return round(total, 2)

    if any(x in q for x in ["void", "voided"]):
        if any(x in q for x in ["how many", "count", "number of"]):
            return sum(1 for o in selected if o.get("status") == "void")
        return round(sum(usd_amount(o) for o in selected if o.get("status") == "void"), 2)

    if any(x in q for x in ["revenue", "sales", "sales total", "total sales", "income", "spent", "spend", "paid amount"]):
        return round(sum(usd_amount(o) for o in selected if o.get("status") == "paid"), 2)

    if "customer" in q and any(x in q for x in ["how many", "number of", "count", "unique"]):
        return len({o.get("customer") for o in selected if o.get("customer")})

    if "product" in q and any(x in q for x in ["how many", "number of", "count", "unique", "list"]):
        return sorted({o.get("product") for o in selected if o.get("product")})

    # Conservative fallback: count matching rows rather than inventing a result.
    return len(selected)

@app.get("/")
def root():
    return {"status": "ok", "service": "ledger-agent", "orders_loaded": len(orders)}

@app.post("/answer")
def answer(payload: Question):
    try:
        result = answer_question(payload.question)
        return {"answer": result}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
