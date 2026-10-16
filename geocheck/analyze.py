"""
Step 4: Decide which location claims are physically possible.

For every claim (one IP, one database), compare:
  - max_distance_km:     the farthest the server can be, from your fastest RTT
                         (RTT / 2 x 200 km/ms, the speed of light in fiber)
  - claimed_distance_km: the distance from your vantage point to the claimed
                         location (a city's coordinates, or a country's
                         closest edge)

If the claim is farther than physics allows, it is "impossible".

Claims come from two databases (results/claims.csv) and, for VPN exit IPs,
from the VPN provider's advertised location (data/vpn_targets.csv). The
provider's location is treated as a claim to test, never as ground truth.

Because RIPE anchors have published locations, the script also:
  - validates the method: every TRUE location should pass the test
  - measures how wrong each claim was (km from the true location, and
    whether the country was right)

Inputs:  data/targets.csv, data/vpn_targets.csv, results/measurements.csv,
         results/claims.csv, data/countries.geojson (Natural Earth shapes)
Outputs: results/analysis.csv (one row per claim)
         results/summary.csv  (one row per IP)

Usage:
    python geocheck/analyze.py
"""

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = ROOT / "data" / "targets.csv"
VPN_TARGETS_PATH = ROOT / "data" / "vpn_targets.csv"
COUNTRIES_PATH = ROOT / "data" / "countries.geojson"
MEASUREMENTS_PATH = ROOT / "results" / "measurements.csv"
CLAIMS_PATH = ROOT / "results" / "claims.csv"
OUT_PATH = ROOT / "results" / "analysis.csv"
SUMMARY_PATH = ROOT / "results" / "summary.csv"

EARTH_RADIUS_KM = 6371.0
FIBER_KM_PER_MS = 200.0  # one-way speed of light in fiber, about 2/3 of c
# Tolerance for small inaccuracies: vantage rounded to ~1 km, city coordinates
# are city centers, and country borders are simplified at 1:50m map scale.
MARGIN_KM = 50.0

FIELDS = [
    "ip", "target_type", "anchor_id", "source", "claim", "claim_country",
    "min_rtt_ms", "max_distance_km", "claimed_distance_km", "verdict",
    "true_city", "true_country", "truth_check", "error_km", "country_correct",
    "vantage_label",
]

Row = dict[str, Any]
Vantage = tuple[float, float, str]  # lat, lon, label


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points on Earth, in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def max_distance_km(rtt_ms: float) -> float:
    """Farthest a server can be: half the round trip, at fiber light speed."""
    return rtt_ms / 2 * FIBER_KM_PER_MS


def load_csv(path: Path) -> list[Row]:
    """Read a CSV file into a list of dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_vpn_claims() -> list[Row]:
    """Turn each VPN provider's advertised location into a claim row."""
    if not VPN_TARGETS_PATH.exists():
        return []
    claims: list[Row] = []
    for v in load_csv(VPN_TARGETS_PATH):
        claims.append({
            "ip": v["ip"],
            "anchor_id": "",
            "source": f"{v['provider'].lower().replace(' ', '-')}-advertised",
            "status": "ok",
            "country_code": v["claimed_country"],
            "city": v["claimed_city"],
            "lat": v["claimed_lat"],
            "lon": v["claimed_lon"],
        })
    return claims


def load_countries() -> dict[str, BaseGeometry]:
    """Load country shapes keyed by 2-letter ISO code."""
    with open(COUNTRIES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    countries: dict[str, BaseGeometry] = {}
    for feature in data["features"]:
        props = feature["properties"]
        code = props.get("ISO_A2_EH") or props.get("ISO_A2")
        if code and code != "-99":
            # segmentize adds points along long straight border lines,
            # so "closest border point" is accurate to within ~10 km.
            countries[code] = shape(feature["geometry"]).segmentize(0.1)
    return countries


def distance_to_country_km(lat: float, lon: float, country: BaseGeometry) -> float:
    """Distance from a point to the closest edge of a country (0 if inside)."""
    if country.contains(Point(lon, lat)):  # shapely uses (x=lon, y=lat)
        return 0.0
    polygons: list[Polygon] = []
    if isinstance(country, Polygon):
        polygons = [country]
    elif isinstance(country, MultiPolygon):
        polygons = list(country.geoms)
    best = math.inf
    for poly in polygons:
        for x, y in poly.exterior.coords:
            best = min(best, haversine_km(lat, lon, y, x))
    return best


def best_rtts(measurements: list[Row]) -> dict[str, list[tuple[Vantage, float]]]:
    """Lowest RTT per IP per vantage point, across all measurement runs."""
    best: dict[tuple[str, Vantage], float] = {}
    for m in measurements:
        if m["min_rtt_ms"] == "":
            continue
        vantage = (float(m["vantage_lat"]), float(m["vantage_lon"]), m["vantage_label"])
        key = (m["ip"], vantage)
        rtt = float(m["min_rtt_ms"])
        if key not in best or rtt < best[key]:
            best[key] = rtt
    by_ip: dict[str, list[tuple[Vantage, float]]] = defaultdict(list)
    for (ip, vantage), rtt in best.items():
        by_ip[ip].append((vantage, rtt))
    return by_ip


def analyze_claim(
    claim: Row,
    rtts: list[tuple[Vantage, float]],
    target: Row | None,
    countries: dict[str, BaseGeometry],
) -> Row:
    """Apply the speed-of-light test to one claim, plus truth checks for anchors."""
    is_city_claim = claim["lat"] != ""
    label = (
        f"{claim['city']}, {claim['country_code']}" if is_city_claim else claim["country_code"]
    )
    out: Row = {
        "ip": claim["ip"], "target_type": "anchor" if target is not None else "vpn",
        "anchor_id": claim["anchor_id"], "source": claim["source"],
        "claim": label, "claim_country": claim["country_code"],
        "min_rtt_ms": "", "max_distance_km": "", "claimed_distance_km": "",
        "verdict": "", "true_city": "", "true_country": "", "truth_check": "",
        "error_km": "", "country_correct": "", "vantage_label": "",
    }

    if claim["status"] != "ok":
        out["verdict"] = "no claim"
        return out
    if not rtts:
        out["verdict"] = "untestable (no response)"
        return out
    country = countries.get(claim["country_code"])
    if not is_city_claim and country is None:
        out["verdict"] = "untestable (unknown country)"
        return out

    # With several vantage points, keep the one that gives the strictest test:
    # the smallest "slack" between what physics allows and what was claimed.
    strictest: tuple[float, Vantage, float, float, float] | None = None
    for vantage, rtt in rtts:
        v_lat, v_lon, _ = vantage
        allowed = max_distance_km(rtt)
        if is_city_claim:
            claimed = haversine_km(v_lat, v_lon, float(claim["lat"]), float(claim["lon"]))
        else:
            assert country is not None
            claimed = distance_to_country_km(v_lat, v_lon, country)
        slack = allowed - claimed
        if strictest is None or slack < strictest[0]:
            strictest = (slack, vantage, rtt, allowed, claimed)

    assert strictest is not None
    slack, vantage, rtt, allowed, claimed = strictest
    out.update({
        "min_rtt_ms": round(rtt, 1),
        "max_distance_km": round(allowed),
        "claimed_distance_km": round(claimed),
        "verdict": "impossible" if slack < -MARGIN_KM else "consistent",
        "vantage_label": vantage[2],
    })

    # Ground-truth checks, available because anchors have published locations.
    if target is not None and target.get("lat"):
        t_lat, t_lon = float(target["lat"]), float(target["lon"])
        v_lat, v_lon, _ = vantage
        true_distance = haversine_km(v_lat, v_lon, t_lat, t_lon)
        out["true_city"] = target["city"]
        out["true_country"] = target["country"]
        # Method validation: the TRUE location must never be "impossible".
        out["truth_check"] = "pass" if true_distance <= allowed + MARGIN_KM else "FAIL"
        out["country_correct"] = claim["country_code"] == target["country"]
        if is_city_claim:
            out["error_km"] = round(
                haversine_km(float(claim["lat"]), float(claim["lon"]), t_lat, t_lon)
            )
    return out


def write_summary(rows: list[Row]) -> None:
    """Write summary.csv: one row per IP, each database's claim and verdict side
    by side, sorted so problems (FAIL, impossible, untestable) come first."""
    sources = sorted({r["source"] for r in rows})
    by_ip: dict[str, Row] = {}
    for r in rows:
        s = by_ip.setdefault(r["ip"], {
            "ip": r["ip"], "target_type": r["target_type"],
            "true_location": "unknown (VPN exit)" if r["target_type"] == "vpn" else "",
            "min_rtt_ms": "", "method_check": "",
        })
        # Fill shared columns from whichever row has them (a failed lookup won't).
        if r["true_city"] and not s["true_location"]:
            s["true_location"] = f"{r['true_city']}, {r['true_country']}"
        if r["min_rtt_ms"] != "" and s["min_rtt_ms"] == "":
            s["min_rtt_ms"] = r["min_rtt_ms"]
        if r["truth_check"] and not s["method_check"]:
            s["method_check"] = r["truth_check"]
        s[f"{r['source']}_claim"] = r["claim"]
        s[f"{r['source']}_verdict"] = r["verdict"]

    def priority(s: Row) -> int:
        verdicts = [str(s.get(f"{src}_verdict", "")) for src in sources]
        if s["method_check"] == "FAIL":
            return 0
        if "impossible" in verdicts:
            return 1
        if any(v.startswith("untestable") or v == "no claim" for v in verdicts):
            return 2
        return 3

    fields = ["ip", "target_type", "true_location", "min_rtt_ms", "method_check"]
    fields += [f"{src}_{col}" for src in sources for col in ("claim", "verdict")]
    with open(SUMMARY_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, restval="")
        writer.writeheader()
        writer.writerows(sorted(by_ip.values(), key=priority))


def print_summary(rows: list[Row]) -> None:
    """Print method validation and per-database results."""
    checked = [r for r in rows if r["truth_check"]]
    passed = sum(1 for r in checked if r["truth_check"] == "pass")
    print(f"\nMethod validation: {passed}/{len(checked)} true locations passed the test")
    if passed < len(checked):
        print("  Some true locations FAILED. Investigate these before trusting results.")

    for source in sorted({r["source"] for r in rows}):
        group = [r for r in rows if r["source"] == source]
        tested = [r for r in group if r["verdict"] in ("consistent", "impossible")]
        impossible = sum(1 for r in tested if r["verdict"] == "impossible")
        right = [r for r in group if r["country_correct"] != ""]
        correct = sum(1 for r in right if r["country_correct"])
        print(f"\n{source}:")
        print(f"  impossible claims: {impossible}/{len(tested)} testable")
        print(f"  correct country:   {correct}/{len(right)}")
        errors = [r["error_km"] for r in group if r["error_km"] != ""]
        if errors:
            print(f"  median error:      {statistics.median(errors):.0f} km from true location")


def main() -> None:
    """Join measurements, claims, and ground truth, then write analysis.csv."""
    targets = {t["ip"]: t for t in load_csv(TARGETS_PATH)}
    rtts_by_ip = best_rtts(load_csv(MEASUREMENTS_PATH))
    claims = load_csv(CLAIMS_PATH) + load_vpn_claims()
    countries = load_countries()
    n_ips = len({c["ip"] for c in claims})
    print(f"Analyzing {len(claims)} claims across {n_ips} IPs...")

    rows = [
        analyze_claim(c, rtts_by_ip.get(c["ip"], []), targets.get(c["ip"]), countries)
        for c in claims
    ]

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    write_summary(rows)
    print_summary(rows)
    print(f"\nFull details: {OUT_PATH}")
    print(f"Summary:      {SUMMARY_PATH}")


if __name__ == "__main__":
    main()