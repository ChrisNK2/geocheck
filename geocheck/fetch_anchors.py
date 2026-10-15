"""
Step 1: Fetch RIPE Atlas anchors (servers with published coordinates)
and save a geographically spread sample to data/targets.csv.

Usage:
    python geocheck/fetch_anchors.py            # 50 anchors, spread across countries
    python geocheck/fetch_anchors.py --n 10     # small test set
    python geocheck/fetch_anchors.py --all      # every active anchor
"""

import argparse
import csv
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

API_URL = "https://atlas.ripe.net/api/v2/anchors/"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "targets.csv"
HEADERS = {"User-Agent": "geocheck-student-project (github.com/ChrisNK2)"}
FIELDS = ["anchor_id", "fqdn", "ip", "asn", "city", "country", "lat", "lon"]

Anchor = dict[str, Any]  # one anchor record as returned by the RIPE API
Row = dict[str, Any]  # one row of targets.csv


def fetch_all_anchors() -> list[Anchor]:
    """Follow the API's 'next' links until every page is collected."""
    anchors: list[Anchor] = []
    url: str | None = API_URL
    while url:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        anchors.extend(data["results"])
        url = data.get("next")
        print(f"  fetched {len(anchors)} anchors so far...")
        time.sleep(1)  # be polite to RIPE's servers
    return anchors


def is_usable(a: Anchor) -> bool:
    """Keep only active anchors with an IPv4 address and coordinates."""
    return bool(
        not a.get("is_disabled")
        and a.get("date_decommissioned") is None
        and a.get("ip_v4")
        and a.get("geometry", {}).get("coordinates")
    )


def to_row(a: Anchor) -> Row:
    """Convert one API anchor record into a targets.csv row."""
    lon, lat = a["geometry"]["coordinates"]  # GeoJSON order is [lon, lat]
    return {
        "anchor_id": a["id"],
        "fqdn": a.get("fqdn", ""),
        "ip": a["ip_v4"],
        "asn": a.get("as_v4", ""),
        "city": a.get("city", ""),
        "country": a.get("country", ""),
        "lat": lat,
        "lon": lon,
    }


def spread_sample(rows: list[Row], n: int, seed: int = 42) -> list[Row]:
    """Round-robin across countries so no single country dominates."""
    rng = random.Random(seed)  # fixed seed = reproducible sample
    by_country: defaultdict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_country[r["country"]].append(r)
    for group in by_country.values():
        rng.shuffle(group)
    countries = sorted(by_country)
    rng.shuffle(countries)

    picked: list[Row] = []
    while len(picked) < n and any(by_country.values()):
        for c in countries:
            if by_country[c] and len(picked) < n:
                picked.append(by_country[c].pop())
    return picked


def main() -> None:
    """Fetch anchors, filter and sample them, and write data/targets.csv."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50, help="number of anchors to keep")
    parser.add_argument("--all", action="store_true", help="keep every active anchor")
    args = parser.parse_args()

    print("Fetching RIPE Atlas anchors...")
    raw = fetch_all_anchors()
    rows = [to_row(a) for a in raw if is_usable(a)]
    print(f"{len(rows)} usable anchors out of {len(raw)} total")

    selected = rows if args.all else spread_sample(rows, args.n)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(selected)

    n_countries = len({r["country"] for r in selected})
    print(f"Saved {len(selected)} anchors from {n_countries} countries to {OUT_PATH}")


if __name__ == "__main__":
    main()