"""
Export data from the Python pipeline for the browser checker (checker/index.html).

This is the link between the two halves of geocheck:
  - known_exits.json: VPN exit IPs from data/vpn_targets.csv, so the checker
    can recognize a visitor coming from a measured VPN server.
  - baselines.json:   how much slower real internet paths are than the speed
    of light, computed from YOUR anchor measurements. The checker uses this to
    estimate what a genuinely local visitor's latency should look like.

Usage:
    python geocheck/export_checker_data.py
"""

import csv
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = ROOT / "data" / "targets.csv"
VPN_TARGETS_PATH = ROOT / "data" / "vpn_targets.csv"
MEASUREMENTS_PATH = ROOT / "results" / "measurements.csv"
OUT_DIR = ROOT / "checker" / "data"

EARTH_RADIUS_KM = 6371.0
KM_PER_MS_RTT = 100.0  # 200 km/ms one way in fiber = 100 km per ms of round trip

# Fallbacks if there is not enough anchor data yet.
DEFAULT_INFLATION_MEDIAN = 1.8
DEFAULT_INFLATION_P90 = 2.5
# Assumed fixed delay a browser adds on top of the network path (Wi-Fi, HTTP,
# TLS processing). Your VPN-off probe run showed about 15-20 ms to Virginia
# beyond what distance alone explains. This is an assumption, not a measurement.
LOCAL_OVERHEAD_MS = 20.0

Row = dict[str, Any]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points on Earth, in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def load_csv(path: Path) -> list[Row]:
    """Read a CSV file into a list of dicts (empty list if the file is missing)."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def export_known_exits() -> list[Row]:
    """VPN exits with their provider and advertised location."""
    exits: list[Row] = []
    for v in load_csv(VPN_TARGETS_PATH):
        exits.append({
            "ip": v["ip"],
            "provider": v["provider"],
            "server": v["server"],
            "claimed_city": v["claimed_city"],
            "claimed_country": v["claimed_country"],
        })
    return exits


def compute_baselines() -> Row:
    """Path inflation = measured RTT / fastest physically possible RTT.

    Uses only anchors (true locations known) and the best RTT per anchor
    per vantage point. Anchors closer than 500 km are skipped, because over
    short distances fixed delays dominate and the ratio becomes meaningless.
    """
    anchors = {t["ip"]: t for t in load_csv(TARGETS_PATH)}
    best: dict[tuple[str, float, float], float] = {}
    for m in load_csv(MEASUREMENTS_PATH):
        if m["ip"] not in anchors or m["min_rtt_ms"] == "":
            continue
        key = (m["ip"], float(m["vantage_lat"]), float(m["vantage_lon"]))
        rtt = float(m["min_rtt_ms"])
        if key not in best or rtt < best[key]:
            best[key] = rtt

    ratios: list[float] = []
    for (ip, v_lat, v_lon), rtt in best.items():
        a = anchors[ip]
        distance = haversine_km(v_lat, v_lon, float(a["lat"]), float(a["lon"]))
        if distance < 500:
            continue
        ratios.append(rtt / (distance / KM_PER_MS_RTT))

    if len(ratios) >= 3:
        median = statistics.median(ratios)
        p90 = statistics.quantiles(ratios, n=10)[-1]
        source = "measured"
    else:
        median, p90 = DEFAULT_INFLATION_MEDIAN, DEFAULT_INFLATION_P90
        source = "default (fewer than 3 anchors measured)"

    return {
        "inflation_median": round(median, 2),
        "inflation_p90": round(p90, 2),
        "anchors_used": len(ratios),
        "inflation_source": source,
        "local_overhead_ms": LOCAL_OVERHEAD_MS,
        "local_overhead_source": "assumed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> None:
    """Write known_exits.json and baselines.json into checker/data/."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    exits = export_known_exits()
    with open(OUT_DIR / "known_exits.json", "w", encoding="utf-8") as f:
        json.dump(exits, f, indent=2)
    print(f"known_exits.json: {len(exits)} VPN exits")

    baselines = compute_baselines()
    with open(OUT_DIR / "baselines.json", "w", encoding="utf-8") as f:
        json.dump(baselines, f, indent=2)
    print(
        f"baselines.json: inflation median {baselines['inflation_median']}x, "
        f"p90 {baselines['inflation_p90']}x from {baselines['anchors_used']} anchors "
        f"({baselines['inflation_source']})"
    )
    print(f"Saved to {OUT_DIR}")


if __name__ == "__main__":
    main()
