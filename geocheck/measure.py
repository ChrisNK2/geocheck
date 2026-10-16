"""
Step 2: Measure the minimum round-trip time (RTT) from this machine to every
target in data/targets.csv and data/vpn_targets.csv, and save the results to results/measurements.csv.

Method: ICMP ping first (minimum of N replies). If ping gets no replies,
fall back to timing TCP connection setup on port 443, then port 80.
A TCP handshake (SYN -> SYN-ACK) takes about one round trip.

Your location (the vantage point) is detected automatically with macOS
Location Services via CoreLocationCLI, which uses Wi-Fi positioning rather
than IP geolocation. If detection fails, the script stops instead of guessing;
pass --lat and --lon to set it manually.

Each run is appended, so you can measure at different times of day;
the analysis step uses the lowest RTT seen across all runs.

Usage:
    python geocheck/measure.py
    python geocheck/measure.py --count 10 --workers 4
    python geocheck/measure.py --lat 40.71 --lon -74.01 --label "New York"
    python geocheck/measure.py --overwrite      # start a fresh file
"""

import argparse
import csv
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = ROOT / "data" / "targets.csv"
VPN_TARGETS_PATH = ROOT / "data" / "vpn_targets.csv"
OUT_PATH = ROOT / "results" / "measurements.csv"

# Coordinates are rounded to 2 decimals (about 1 km) so your exact
# location never ends up in a public repo. 1 km is negligible next to
# the thousands of km the analysis deals with.
COORD_DECIMALS = 2

RTT_PATTERN = re.compile(r"time[=<]([\d.]+)\s*ms")
FIELDS = [
    "anchor_id", "ip", "method", "min_rtt_ms", "replies", "sent",
    "measured_at_utc", "vantage_label", "vantage_lat", "vantage_lon", "vantage_source",
]

Row = dict[str, Any]
Vantage = tuple[float, float, str, str]  # lat, lon, label, source


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


def detect_location() -> tuple[float, float] | None:
    """Ask macOS Location Services for this computer's coordinates."""
    try:
        out = subprocess.run(
            ["CoreLocationCLI"], capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    parts = out.stdout.split()  # default output format: "<lat> <lon>"
    try:
        return float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        return None


def resolve_vantage(lat: float | None, lon: float | None, label: str | None) -> Vantage:
    """Use manual coordinates if given, otherwise detect them. Never guess."""
    if (lat is None) != (lon is None):
        sys.exit("Pass both --lat and --lon, or neither.")
    if lat is not None and lon is not None:
        source = "manual"
    else:
        found = detect_location()
        if found is None:
            sys.exit(
                "Could not detect your location. Check that Wi-Fi and Location Services\n"
                "are on and CoreLocationCLI is installed, or pass --lat and --lon."
            )
        lat, lon = found
        source = "corelocation"
    lat, lon = round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)
    return lat, lon, label or f"{lat}, {lon}", source


def ping_rtts(ip: str, count: int) -> list[float]:
    """Return RTTs in ms from ICMP ping replies (empty list if none)."""
    timeout_s = count + 3
    # The overall-timeout flag differs: -t on macOS, -w on Linux.
    timeout_flag = "-t" if sys.platform == "darwin" else "-w"
    cmd = ["ping", "-c", str(count), timeout_flag, str(timeout_s), ip]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s + 5, check=False
        )
    except subprocess.TimeoutExpired:
        return []
    return [float(t) for t in RTT_PATTERN.findall(out.stdout)]


def tcp_rtts(ip: str, count: int, timeout_s: float = 3.0) -> tuple[list[float], int | None]:
    """Time TCP connection setup (about one RTT). Returns (rtts_ms, port used)."""
    for port in (443, 80):
        rtts: list[float] = []
        for _ in range(count):
            start = time.perf_counter()
            try:
                with socket.create_connection((ip, port), timeout=timeout_s):
                    pass
            except ConnectionRefusedError:
                pass  # a refusal (RST) still took one full round trip
            except OSError:
                continue  # timeout or unreachable: no timing to record
            rtts.append((time.perf_counter() - start) * 1000)
            time.sleep(0.2)
        if rtts:
            return rtts, port
    return [], None


def measure_target(target: Row, count: int, vantage: Vantage) -> Row:
    """Measure one target: ping first, TCP timing as a fallback."""
    ip = target["ip"]
    rtts = ping_rtts(ip, count)
    method = "icmp"
    if not rtts:
        rtts, port = tcp_rtts(ip, count)
        method = f"tcp/{port}" if port else "none"

    lat, lon, label, source = vantage
    return {
        "anchor_id": target.get("anchor_id", ""),
        "ip": ip,
        "method": method,
        "min_rtt_ms": round(min(rtts), 3) if rtts else "",
        "replies": len(rtts),
        "sent": count,
        "measured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vantage_label": label,
        "vantage_lat": lat,
        "vantage_lon": lon,
        "vantage_source": source,
    }


def main() -> None:
    """Measure every target in targets.csv and append results to measurements.csv."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5, help="probes per target")
    parser.add_argument("--workers", type=int, default=8, help="targets measured at once")
    parser.add_argument("--lat", type=float, help="your latitude (skips auto-detect)")
    parser.add_argument("--lon", type=float, help="your longitude (skips auto-detect)")
    parser.add_argument("--label", help="name for this vantage point, e.g. Boston")
    parser.add_argument("--overwrite", action="store_true", help="replace instead of append")
    args = parser.parse_args()

    vantage = resolve_vantage(args.lat, args.lon, args.label)
    print(f"Vantage point: {vantage[2]} ({vantage[0]}, {vantage[1]}) via {vantage[3]}")

    targets = load_targets()
    print(f"Measuring {len(targets)} targets ({args.count} probes each)...")

    results: list[Row] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(measure_target, t, args.count, vantage) for t in targets]
        for i, fut in enumerate(as_completed(futures), start=1):
            r = fut.result()
            results.append(r)
            rtt = f"{r['min_rtt_ms']} ms" if r["min_rtt_ms"] != "" else "no response"
            print(f"  [{i}/{len(targets)}] {r['ip']:<16} {r['method']:<8} {rtt}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = args.overwrite or not OUT_PATH.exists()
    mode = "w" if args.overwrite else "a"
    with open(OUT_PATH, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(results)

    answered = sum(1 for r in results if r["min_rtt_ms"] != "")
    print(f"{answered}/{len(results)} targets responded. Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()