#!/usr/bin/env python3
"""
bank_statement_import.py

Parses an ACLEDA-style statement (.xlsx) and imports transactions directly
into a self-hosted Sure (we-promise/sure) instance via its REST API.
Deterministic fields (date, amount, currency, ref) are parsed with regex -
free, instant, no LLM. Merchant name cleanup and category assignment are
left to Sure itself: as of v0.6.6 Sure's own background enrichment reads
transaction notes to determine merchant/category (when AI is enabled for
the family), so this script does not call any LLM.

Dedup is handled by Sure's API itself: each transaction is created with an
`external_id` (the statement's REF#, or a hash of the row if no REF# is
present) and a fixed `source` namespace. Re-POSTing the same external_id
returns the existing transaction (HTTP 200) instead of creating a
duplicate (HTTP 201), so re-running on an overlapping statement date range
is safe.

Usage:
    python3 bank_statement_import.py /path/to/statement.xlsx

Env vars required:
    SURE_API_URL     base URL of the Sure instance, e.g. https://sure.example.com
    SURE_API_KEY     Sure API key with read_write scope (Settings -> API keys)
    SURE_ACCOUNT_ID  UUID of the Sure account to import into
                      -- OR --
    SURE_ACCOUNT_NAME  exact name of the Sure account to import into
                        (looked up via GET /api/v1/accounts; set this
                        instead of SURE_ACCOUNT_ID if you don't have the UUID)

Optional:
    SURE_IMPORT_SOURCE  external_id namespace, default "acleda_statement_import"

Exit: prints a JSON summary to stdout (rows_imported, rows_skipped_dupe,
rows_failed) so n8n's "Execute Command" node can parse it directly into a
Telegram message.
"""

import os
import re
import sys
import json
import hashlib
import urllib.request
import urllib.error
from datetime import datetime

SURE_API_URL = os.environ.get("SURE_API_URL", "").rstrip("/")
SURE_API_KEY = os.environ.get("SURE_API_KEY", "")
SURE_ACCOUNT_ID = os.environ.get("SURE_ACCOUNT_ID", "")
SURE_ACCOUNT_NAME = os.environ.get("SURE_ACCOUNT_NAME", "")
IMPORT_SOURCE = os.environ.get("SURE_IMPORT_SOURCE", "acleda_statement_import")


# ---------- 1. Deterministic parsing (no LLM) ----------

def parse_raw_name(details: str) -> str:
    d = details
    m = re.match(r'PURCHASE AT ([^,]+?)-No:', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'PURCHASE AT ([^,]+),', d)
    if m and 'CARD#' in d:
        return m.group(1).strip()
    m = re.match(r'PURCHASE AT (.+?) ON ', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'FUNDS TRANSFERRED TO (.+?) \d', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'FUNDS RECEIVED FROM (.+?) \(', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'PAYMENT TO (.+?) \(PIN-less\)', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'PAYMENT TO (.+?) \d', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'PAYMENT FROM ([^/]+)/', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'PAYMENT FROM (.+?) \d', d)
    if m:
        return m.group(1).strip()
    m = re.match(r'CASH DEPOSIT FROM QR DEPOSIT TO OWN ACCOUNT', d)
    if m:
        return "Cash Deposit (QR)"
    m = re.match(r'CASH DEPOSIT AT (.+?) KHR-', d)
    if m:
        return m.group(1).strip()
    return d[:40].strip()


def parse_statement(path: str):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Sheet1"]
    out = []
    for row in ws.iter_rows(min_row=4, max_col=8, values_only=True):
        date_raw = row[0]
        if not date_raw:
            continue
        details = (row[1] or "").strip()
        money_in = row[2]
        money_out = row[4]
        ccy = row[5] or row[3] or "USD"

        dt = datetime.strptime(date_raw, "%b %d, %Y")
        mi = float(str(money_in).replace(",", "")) if money_in else 0.0
        mo = float(str(money_out).replace(",", "")) if money_out else 0.0
        is_income = mi > 0
        amount = round(mi if is_income else mo, 2)

        ref_match = re.search(r"REF# (\S+)", details)
        ref = ref_match.group(1) if ref_match else hashlib.sha1(details.encode()).hexdigest()[:16]

        raw_name = parse_raw_name(details)

        out.append({
            "ref": ref,
            "date": dt.strftime("%Y-%m-%d"),
            "amount": amount,
            "currency": ccy,
            "name": raw_name,
            "is_income": is_income,
            "notes": details,
        })
    return out


# ---------- 2. Sure API client ----------

class SureApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


def _request(method: str, path: str, payload: dict = None):
    url = f"{SURE_API_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Api-Key": SURE_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Cloudflare's WAF blocks the default Python-urllib/x.y User-Agent outright.
        "User-Agent": "bank-statement-import/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {"error": body.decode(errors="replace")}
        raise SureApiError(e.code, body)


def resolve_account_id() -> str:
    if SURE_ACCOUNT_ID:
        return SURE_ACCOUNT_ID
    if not SURE_ACCOUNT_NAME:
        raise RuntimeError("Set SURE_ACCOUNT_ID or SURE_ACCOUNT_NAME")

    page = 1
    while True:
        status, body = _request("GET", f"/api/v1/accounts?per_page=100&page={page}")
        for acct in body.get("accounts", []):
            if acct["name"].strip().lower() == SURE_ACCOUNT_NAME.strip().lower():
                return acct["id"]
        pagination = body.get("pagination", {})
        if page >= pagination.get("total_pages", 1):
            break
        page += 1

    raise RuntimeError(f"No Sure account found matching SURE_ACCOUNT_NAME={SURE_ACCOUNT_NAME!r}")


def create_transaction(row: dict, account_id: str):
    payload = {
        "transaction": {
            "account_id": account_id,
            "date": row["date"],
            "amount": row["amount"],
            "name": row["name"],
            "notes": row["notes"],
            "currency": row["currency"],
            "nature": "income" if row["is_income"] else "expense",
            "external_id": row["ref"],
            "source": IMPORT_SOURCE,
        }
    }
    return _request("POST", "/api/v1/transactions", payload)


# ---------- 3. Main ----------

def main():
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: bank_statement_import.py <statement.xlsx>"}))
        sys.exit(1)

    if not SURE_API_URL or not SURE_API_KEY:
        print(json.dumps({"error": "SURE_API_URL and SURE_API_KEY must be set"}))
        sys.exit(1)

    path = sys.argv[1]
    rows = parse_statement(path)

    try:
        account_id = resolve_account_id()
    except (RuntimeError, SureApiError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    imported, skipped_dupe, failed = 0, 0, 0
    errors = []
    for r in rows:
        try:
            status, _body = create_transaction(r, account_id)
            if status == 201:
                imported += 1
            elif status == 200:
                skipped_dupe += 1
            else:
                failed += 1
                errors.append({"ref": r["ref"], "status": status})
        except SureApiError as e:
            failed += 1
            errors.append({"ref": r["ref"], "status": e.status, "error": e.body})

    summary = {
        "rows_imported": imported,
        "rows_skipped_dupe": skipped_dupe,
        "rows_failed": failed,
    }
    if errors:
        summary["errors"] = errors[:10]  # cap so a bad statement doesn't flood the Telegram reply
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
