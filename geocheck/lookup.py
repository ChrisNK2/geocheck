"""
Step 3: Ask two IP geolocation databases where each target IP is, and save
their claims to results/claims.csv (one row per IP per database).

Databases:
  - ip-api (free, no key): city-level claim with coordinates.
    Batch endpoint, up to 100 IPs per request, 15 requests/minute limit.
  - IPinfo Lite (free, needs token): country-level claim only.
    Reads the token from the IPINFO_TOKEN environment variable.

Usage:
    python geocheck/lookup.py
    python geocheck/lookup.py --only ip-api      # skip IPinfo
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = ROOT / "data" / "targets.csv"
VPN_TARGETS_PATH = ROOT / "data" / "vpn_targets.csv"
OUT_PATH = ROOT / "results" / "claims.csv"

IPAPI_URL = "http://ip-api.com/batch"  # free tier is HTTP only
IPAPI_FIELDS = "status,message,country,countryCode,city,lat,lon,as,query"
IPAPI_BATCH_SIZE = 100
IPAPI_PAUSE_S = 4.5  # 15 requests/minute = one every 4 seconds, plus margin

IPINFO_URL = "https://api.ipinfo.io/lite/{ip}"
IPINFO_PAUSE_S = 0.1

FIELDS = [
    "anchor_id", "ip", "source", "status", "country_code", "country",
    "city", "lat", "lon", "asn", "as_name", "looked_up_at_utc",
]

Row = dict[str, Any]


def load_targets() -> list[Row]:
    """Anchors from targets.csv plus VPN exits from vpn_targets.csv (if present).

    VPN rows get a blank anchor_id. Duplicate IPs are kept only once.
    """
    with open(TARGETS_PATH, newline="", encoding="utf-8") as f:
        rows: list[Row] = list(csv.DictReader(f))
    if VPN_TARGETS_PATH.exists():
        with open(VPN_TARGETS_PATH, newline="", encoding="utf-8") as f:
            rows += [{"ip": v["ip"], "anchor_id": ""} for v in csv.DictReader(f)]
    seen: set[str] = set()
    unique: list[Row] = []
    for r in rows:
        if r["ip"] not in seen:
            seen.add(r["ip"])
            unique.append(r)
    return unique


def now_utc() -> str:
    """Current time as an ISO 8601 UTC string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lookup_ipapi(ips: list[str]) -> dict[str, Row]:
    """Query ip-api's batch endpoint. Returns {ip: claim}."""
    claims: dict[str, Row] = {}
    for start in range(0, len(ips), IPAPI_BATCH_SIZE):
        chunk = ips[start:start + IPAPI_BATCH_SIZE]
        resp = requests.post(
            IPAPI_URL, params={"fields": IPAPI_FIELDS}, json=chunk, timeout=30
        )
        if resp.status_code == 429:
            sys.exit("ip-api rate limit hit. Wait a minute and run again.")
        resp.raise_for_status()

        for item in resp.json():
            ok = item.get("status") == "success"
            claims[item["query"]] = {
                "source": "ip-api",
                "status": "ok" if ok else f"fail: {item.get('message', '')}",
                "country_code": item.get("countryCode", ""),
                "country": item.get("country", ""),
                "city": item.get("city", ""),
                "lat": item.get("lat", ""),
                "lon": item.get("lon", ""),
                "asn": item.get("as", ""),
                "as_name": "",
                "looked_up_at_utc": now_utc(),
            }
        print(f"  ip-api: {len(claims)}/{len(ips)} looked up")

        # Respect the rate-limit headers: X-Rl = requests left, X-Ttl = seconds to reset.
        if start + IPAPI_BATCH_SIZE < len(ips):
            if resp.headers.get("X-Rl") == "0":
                time.sleep(int(resp.headers.get("X-Ttl", "60")) + 1)
            else:
                time.sleep(IPAPI_PAUSE_S)
    return claims


def lookup_ipinfo(ips: list[str], token: str) -> dict[str, Row]:
    """Query IPinfo Lite one IP at a time. Returns {ip: claim}."""
    claims: dict[str, Row] = {}
    for i, ip in enumerate(ips, start=1):
        try:
            resp = requests.get(
                IPINFO_URL.format(ip=ip), params={"token": token}, timeout=15
            )
        except requests.RequestException as e:
            # Print only the error type: the full message would include the URL,
            # and the URL contains your token.
            status = f"fail: {type(e).__name__}"
        else:
            status = "ok" if resp.ok else f"fail: HTTP {resp.status_code}"
            if resp.status_code in (401, 403):
                sys.exit("IPinfo rejected the token. Check IPINFO_TOKEN.")

        data: Row = resp.json() if status == "ok" else {}
        claims[ip] = {
            "source": "ipinfo-lite",
            "status": status,
            "country_code": data.get("country_code", ""),
            "country": data.get("country", ""),
            "city": "",  # Lite is country-level only
            "lat": "",
            "lon": "",
            "asn": data.get("asn", ""),
            "as_name": data.get("as_name", ""),
            "looked_up_at_utc": now_utc(),
        }
        if i % 10 == 0 or i == len(ips):
            print(f"  ipinfo-lite: {i}/{len(ips)} looked up")
        time.sleep(IPINFO_PAUSE_S)
    return claims


def main() -> None:
    """Look up every target in both databases and write results/claims.csv."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["ip-api", "ipinfo-lite"], help="use one database")
    args = parser.parse_args()

    targets = load_targets()
    ips = [t["ip"] for t in targets]
    anchor_ids = {t["ip"]: t.get("anchor_id", "") for t in targets}
    print(f"Looking up {len(ips)} IPs...")

    results: list[dict[str, Row]] = []
    if args.only in (None, "ip-api"):
        results.append(lookup_ipapi(ips))
    if args.only in (None, "ipinfo-lite"):
        token = os.environ.get("IPINFO_TOKEN")
        if not token:
            sys.exit("IPINFO_TOKEN is not set. Run: source ~/.zshrc")
        results.append(lookup_ipinfo(ips, token))

    rows: list[Row] = []
    for claims in results:
        for ip in ips:
            if ip in claims:
                rows.append({"anchor_id": anchor_ids[ip], "ip": ip, **claims[ip]})

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"{ok}/{len(rows)} lookups succeeded. Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()