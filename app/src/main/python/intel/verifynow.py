"""VerifyNow API client + response parsers.

Confirmed from VerifyNow's public docs:
  * Base URL      https://www.verifynow.co.za/api/external
  * Auth          x-api-key header
  * Endpoint      POST /verify  {"reportType": ..., "idNumber": ..., "mode": ...}
  * Idempotency   Idempotency-Key header (required in production)
  * reportTypes   "said_verification" and "consumer_trace" are documented.
                  "home_affairs_id_photo" appears on the ID+photo service page.

NOT confirmed publicly (check your VerifyNow dashboard API docs and override
via environment variables if they differ):
  * marital status reportType        -> VERIFYNOW_RT_MARITAL
  * "lite" consumer trace reportType -> VERIFYNOW_RT_TRACE
  * ID+photo reportType              -> VERIFYNOW_RT_PHOTO

The parsers are deliberately tolerant (case-insensitive recursive key search)
and the full raw JSON is always stored, so nothing is lost if a field name
differs from what we expect.
"""
import base64
import binascii
import json
import os
import re
import struct
import time
import uuid
import zlib

import requests


def _load_dotenv():
    """Read KEY=VALUE lines from a .env file next to this file (real env vars win)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if k.strip() and v:
                    os.environ.setdefault(k.strip(), v)
    except FileNotFoundError:
        pass


_load_dotenv()

BASE_URL = os.environ.get("VERIFYNOW_BASE_URL", "https://www.verifynow.co.za/api/external").rstrip("/")
API_KEY = os.environ.get("VERIFYNOW_API_KEY", "")
MODE = os.environ.get("VERIFYNOW_MODE", "sandbox")          # sandbox = 0 credits
MOCK = os.environ.get("VERIFYNOW_MOCK", "") == "1"          # canned responses, no network
TIMEOUT = int(os.environ.get("VERIFYNOW_TIMEOUT", "60"))
SPACING = float(os.environ.get("VERIFYNOW_SPACING", "2.5"))   # min seconds between real API calls (VerifyNow 429s on rapid repeats)
MAX_RETRIES = int(os.environ.get("VERIFYNOW_RETRIES", "3"))   # extra attempts on HTTP 429
_last_call = 0.0

SERVICES = {
    "id_photo": {
        "label": "IDRV",
        "report_type": os.environ.get("VERIFYNOW_RT_PHOTO", "home_affairs_id_photo"),
    },
    "consumer_trace": {
        "label": "CT",
        "report_type": os.environ.get("VERIFYNOW_RT_TRACE", "consumer_trace"),
    },
}

# short codes are all users ever see; AF = all, in this order. PH is handled locally (saved numbers only).
CODES = {"id_photo": "IDRV", "consumer_trace": "CT"}
SEARCH_TYPES = {"IDRV": ["id_photo"], "CT": ["consumer_trace"], "AF": ["id_photo", "consumer_trace"]}   # PH is local

ERRORS = {
    400: "The search service rejected the request (invalid parameters).",
    401: "The search service rejected the API key.",
    402: "SERVICES CURRENTLY UNAVAILABLE",
    409: "Idempotency conflict (same key, different request).",
    429: "Rate limited - wait a few seconds and retry.",
}


def configured():
    return MOCK or bool(API_KEY)


# --------------------------------------------------------------- validation
def valid_sa_id(s):
    """13 digits + Luhn check digit."""
    if not re.fullmatch(r"\d{13}", s or ""):
        return False
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def norm_phone(s):
    """'+27 83 338 7060' / '27833387060' / '083 338 7060' -> '0833387060' (None if not a 10-digit SA number)."""
    d = re.sub(r"\D", "", s or "")
    if d.startswith("27") and len(d) == 11:
        d = "0" + d[2:]
    return d if re.fullmatch(r"0\d{9}", d) else None


def idem_key(token, service, id_number):
    """Same form submission + service + ID -> same key, so a double-click or
    retry returns VerifyNow's cached response instead of charging twice."""
    try:
        ns = uuid.UUID(token)
    except (ValueError, TypeError, AttributeError):
        ns = uuid.uuid4()
    return str(uuid.uuid5(ns, f"{service}:{id_number}"))


# ------------------------------------------------------------------- client
def call(service, value, idempotency_key):
    """value = SA ID number.
    Returns dict: ok, http_status, request_id, remaining_credits, error, raw."""
    rt = SERVICES[service]["report_type"]
    field = SERVICES[service].get("field", "idNumber")
    if MOCK:
        return _mock(service, rt, value)
    if not API_KEY:
        return _fail(0, "The search service is not configured.")
    global _last_call
    r = None
    for attempt in range(MAX_RETRIES + 1):
        # wait at least SPACING s since the last call; back off further on each retry
        wait = SPACING * (attempt + 1) - (time.monotonic() - _last_call)
        if _last_call and wait > 0:
            time.sleep(wait)
        try:
            r = requests.post(
                f"{BASE_URL}/verify",
                json={"reportType": rt, field: value, "mode": MODE},
                headers={"x-api-key": API_KEY, "Content-Type": "application/json",
                         "Idempotency-Key": idempotency_key},   # same key on retry = never charged twice
                timeout=TIMEOUT)
        except requests.RequestException as e:
            _last_call = time.monotonic()
            return _fail(0, f"Could not reach the search service: {e.__class__.__name__}")
        _last_call = time.monotonic()
        if r.status_code != 429:
            break
    try:
        data = r.json()
    except ValueError:
        data = {"_non_json_body": r.text[:500]}
    if r.status_code != 200 or data.get("success") is False:
        msg = ERRORS.get(r.status_code) or "The search service returned an error."
        detail = data.get("error") or data.get("message") if isinstance(data, dict) else None
        if detail and isinstance(detail, str) and r.status_code != 402:
            msg += f" ({detail})"
        out = _fail(r.status_code, msg)
        out["raw"] = data
        return out
    return {"ok": True, "http_status": 200, "request_id": data.get("requestId"),
            "remaining_credits": data.get("remainingCredits"), "error": None, "raw": data}


def _fail(status, msg):
    return {"ok": False, "http_status": status, "request_id": None,
            "remaining_credits": None, "error": msg, "raw": None}


# ------------------------------------------------------------------ parsers
def _n(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def find_first(obj, names):
    """Depth-first search for the first non-empty scalar under any key in names."""
    names = {_n(x) for x in names}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _n(k) in names and isinstance(v, (str, int, float, bool)) and str(v).strip() != "":
                return str(v).strip()
        for v in obj.values():
            hit = find_first(v, names)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_first(v, names)
            if hit is not None:
                return hit
    return None


def parse_identity(raw):
    """Identity fields we store on the person record (works for all 3 services)."""
    return {
        "first_names": find_first(raw, ["Firstnames", "FirstName", "Forenames", "Names"]),
        "surname": find_first(raw, ["Lastname", "Surname"]),
        "date_of_birth": find_first(raw, ["Dob", "DateOfBirth", "BirthDate"]),
        "gender": find_first(raw, ["Gender", "Sex"]),
        "citizenship": find_first(raw, ["Citizenship"]),
        "marital_status": find_first(raw, ["MaritalStatus", "Marital"]),
        "deceased": find_first(raw, ["AliveDeceasedStatus", "DeceasedStatus", "Deceased", "IsDeceased", "VitalStatus"]),
        "date_issued": find_first(raw, ["IdBookIssuedDate", "IdIssuedDate", "IdBookIssueDate", "DateIssued", "IdIssueDate"]),
        "maiden_surname": find_first(raw, ["MaidenSurname", "MaidenName", "Maiden"]),
        "married_surname": find_first(raw, ["MarriedSurname", "MarriedName", "SurnameMarried"]),
        "marriage_date": find_first(raw, ["MarriageDate", "DateOfMarriage", "MarriedDate", "DateMarried"]),
    }


def parse_iddoc(raw):
    """'ID document details' block of the Home Affairs ID report."""
    return {
        "id_card_indicator": find_first(raw, ["IdCardIndicator", "SmartCardIndicator", "CardIndicator"]),
        "id_sequence": find_first(raw, ["IdSequenceNumber", "SequenceNumber"]),
        "id_blocked": find_first(raw, ["IdBlocked", "Blocked"]),
        "match_status": find_first(raw, ["MatchStatus"]),
        "hanis_match": find_first(raw, ["HanisIdMatch", "HanisMatch", "HanisId"]),
        "id_last_updated": find_first(raw, ["LastUpdated", "LastUpdate"]),
    }


def parse_trace_identity(raw):
    """Identity block of the Consumer / Person Trace report."""
    return {
        "title": find_first(raw, ["Title"]),
        "initials": find_first(raw, ["Initials"]),
        "consumer_id": find_first(raw, ["ConsumerId", "ConsumerID"]),
        "privacy_status": find_first(raw, ["PrivacyStatus"]),
        "trace_full_name": find_first(raw, ["FullName", "FullNames"]),
    }


def parse_photo(raw):
    """Find a base64 image anywhere under a key containing 'photo' or 'image'.
    Returns (bytes, ext) or (None, None)."""
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                nk = _n(k)
                if isinstance(v, str) and ("photo" in nk or "image" in nk or "portrait" in nk):
                    got = _decode_image(v)
                    if got:
                        return got
                got = walk(v)
                if got:
                    return got
        elif isinstance(o, list):
            for v in o:
                got = walk(v)
                if got:
                    return got
        return None
    return walk(raw) or (None, None)


def _decode_image(s):
    s = s.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        b = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None
    if b[:3] == b"\xff\xd8\xff":
        return b, "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return b, "png"
    return None


def _collect(obj, hints):
    """Lists of dicts found under keys containing any hint (no double counting)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(h in _n(k) for h in hints) and isinstance(v, (list, dict)):
                items = v if isinstance(v, list) else [v]
                dicts = [i for i in items if isinstance(i, dict)]
                # a wrapper dict like AddressData:{CurrentAddress:{...}} -> descend
                flat = [d for d in dicts if any(not isinstance(x, (dict, list)) for x in d.values())]
                if flat:
                    out.extend(flat)
                    continue
            out.extend(_collect(v, hints))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_collect(v, hints))
    return out


def _row(cat, item):
    """One trace-table row: type / value / postal code / first reported / last updated / occupation."""
    kind = postal = first = last = occ = ""
    parts = []
    for k, v in item.items():
        if isinstance(v, (dict, list)) or v in (None, ""):
            continue
        nk, v = _n(k), str(v).strip()
        if not v or v == "-":
            continue
        if "type" in nk and not kind:
            kind = v
        elif "postal" in nk or nk in ("zip", "zipcode", "postcode"):
            postal = v
        elif "first" in nk and ("report" in nk or "seen" in nk or "date" in nk):
            first = v
        elif "last" in nk and ("update" in nk or "report" in nk or "seen" in nk or "date" in nk):
            last = v
        elif "occupation" in nk or nk in ("jobtitle", "position"):
            occ = v
        else:
            parts.append(v)
    return {"category": cat, "kind": kind, "value": ", ".join(parts), "postal_code": postal,
            "first_reported": first, "last_updated": last, "occupation": occ, "detail": item}


def _inner(raw):
    """Strip the {'results': {<reportType>: {...}}} wrapper so a report-type name that happens to
    contain a keyword (e.g. 'contact_enquiry') is never mistaken for data."""
    cur = raw
    for _ in range(3):
        if isinstance(cur, dict) and isinstance(cur.get("results"), dict):
            cur = cur["results"]
        if isinstance(cur, dict) and len(cur) == 1 and isinstance(next(iter(cur.values())), dict):
            cur = next(iter(cur.values()))
    return cur


def parse_trace(raw):
    """-> list of row dicts for address history, contact information and employment history."""
    raw = _inner(raw)
    rows = []
    for cat, hints in (("address", ["address"]),
                       ("phone", ["contact", "phone", "telephone", "cell", "email"]),
                       ("employer", ["employ"])):
        seen = set()
        for item in _collect(raw, hints):
            r = _row(cat, item)
            key = (r["kind"], r["value"], r["occupation"])
            if not r["value"] or key in seen:
                continue
            seen.add(key)
            rows.append(r)
    return rows


# --------------------------------------------------------------------- mock
def _png(w=120, h=150, shade=170):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    row = b"\x00" + bytes([shade, shade, shade]) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


def _mock(service, rt, value):
    ident = {"Firstnames": "NALEDI LERATO", "Lastname": "DLAMINI", "Dob": "1991-11-06",
             "Gender": "Female", "Citizenship": "South African"}
    if service == "id_photo":
        results = {"Status": "Success", "Verification": ident,
                   "IdDocumentDetails": {"IdBookIssuedDate": "2009-12-01", "IdCardIndicator": "NO",
                                         "IdSequenceNumber": "0", "IdBlocked": "NO",
                                         "MatchStatus": "MATCHED", "HanisIdMatch": "MATCHED",
                                         "LastUpdated": "Greater than 90 days"},
                   "VitalStatus": {"AliveDeceasedStatus": "ALIVE"}}
        results["Photo"] = base64.b64encode(_png()).decode()
    elif service == "marital_status":
        results = {"Status": "Success", "Verification": {
            "Firstnames": ident["Firstnames"], "Lastname": "KHUMALO", "Dob": ident["Dob"],
            "MaritalStatus": "Married", "MarriageDate": "2020-04-18"}}
    else:
        results = {"Status": "Success",
                   "DefiniteMatchData": {"Identity": {
                       "FullName": "NALEDI LERATO KHUMALO", "Initials": "NL", "Title": "Mrs",
                       "Gender": "Female", "Dob": "1991-11-06", "MaritalStatus": "Married",
                       "AliveDeceasedStatus": "ALIVE", "ConsumerId": "100200300",
                       "PrivacyStatus": "ACCEPTS CONTACTS"}},
                   "AddressData": [
                       {"AddressType": "Residential", "Address": "12 JACARANDA STREET ROSEBANK",
                        "PostalCode": "2196", "FirstReported": "2019-03-01", "LastUpdated": "2025-08-14"},
                       {"AddressType": "Postal", "Address": "PO BOX 55 ROSEBANK", "PostalCode": "2196"}],
                   "ContactData": [
                       {"ContactType": "Cellular", "Number": "0821234567", "LastUpdated": "2025-08-14"},
                       {"ContactType": "Work", "Number": "0115550100"}],
                   "EmploymentData": [{"EmployerName": "EXAMPLE RETAIL (PTY) LTD",
                                       "Occupation": "Accountant", "LastUpdated": "2024-11-02"}]}
    raw = {"success": True, "requestId": str(uuid.uuid4()), "remainingCredits": 0,
           "mode": "mock", "reportType": rt, "input": {"value": value},
           "results": {rt: results}}
    return {"ok": True, "http_status": 200, "request_id": raw["requestId"],
            "remaining_credits": 0, "error": None, "raw": raw}


def set_config(api_key=None, mode=None):
    """Change the key / mode at runtime (the Settings page does this; nothing needs restarting)."""
    global API_KEY, MODE
    if api_key is not None:
        API_KEY = api_key.strip()
    if mode in ("sandbox", "production"):
        MODE = mode


# ------------------------------------------------------------- cellphone network
# Prefix allocations (ICASA). Longest prefix wins. Number portability means this is the network the
# number was ORIGINALLY allocated to, so it is shown as a guide only.
_NET = {}
for _net, _prefixes in {
    "Vodacom": "0606 0607 0608 0609 0636 0637 0646 0647 0648 0649 066 0673 0674 0675 0711 0712 0713 0714 0715 0716 072 076 "
               "0771 0772 0773 0774 0775 079 0818 082",
    "MTN": "0603 0604 0605 0630 0631 0632 0633 0634 0635 0640 0655 0656 0657 0686 0687 0688 0689 0690 0710 0717 0718 0719 "
           "073 078 0810 083",
    "Cell C": "061 062 0641 0642 0643 0644 0645 0650 0651 0652 0653 0654 074 084",
    "Telkom": "0601 0602 0658 0659 0670 0671 0672 0676 0677 0678 0679 0680 0681 0682 0683 0684 0685 0691 0692 0693 0694 "
              "0695 0696 0697 0698 0699 0811 0812 0813 0814 0815 0817 0819",
    "rain": "0816",
    "Liquid Telecom": "0600",
}.items():
    for _p in _prefixes.split():
        _NET[_p] = _net

NETWORK_PREFIXES = _NET


def network_of(number):
    """'0821234567' / '+27 82 123 4567' -> 'Vodacom' ('' when not a recognised mobile number)."""
    n = norm_phone(number)
    if not n:
        return ""
    for k in (4, 3):
        if n[:k] in _NET:
            return _NET[n[:k]]
    return ""
