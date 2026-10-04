"""Command-line entry point for the emergency mesh simulator.

Examples (run from the simulation/ folder):
    python run.py demo
    python run.py failures --trials 30
    python run.py all
"""
from __future__ import annotations

import argparse
import csv
import math
import os

from meshsim import RadioConfig, scenarios, visualize


def _fmt(value) -> str:
    return "n/a" if isinstance(value, float) and math.isnan(value) else str(value)


def print_table(rows: list, title: str) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (no rows)")
        return
    headers = list(rows[0].keys())
    widths = {h: max(len(h), *(len(_fmt(r[h])) for r in rows)) for h in headers}
    print("  " + "  ".join(h.ljust(widths[h]) for h in headers))
    print("  " + "  ".join("-" * widths[h] for h in headers))
    for r in rows:
        print("  " + "  ".join(_fmt(r[h]).ljust(widths[h]) for h in headers))


def save_csv(rows: list, path: str) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def run_demo(args, radio, charts):
    result = scenarios.demo(seed=args.seed, radio=radio)
    print("\nDEMO: SOS rerouted around a destroyed relay")
    print(f"  SOS 1 route (node -> base): {' -> '.join(map(str, result.first_path))}")
    print(f"  Relay node {result.killed} destroyed")
    if result.second_path:
        print(f"  SOS 2 route (node -> base): {' -> '.join(map(str, result.second_path))}")
    else:
        print("  SOS 2 was not delivered in time")
    rows = scenarios.dashboard_rows(result.sim)
    print_table(rows, "RESCUE DASHBOARD (sorted by priority; locations are sample Chennai coordinates)")
    save_csv(rows, os.path.join(args.out, "demo_dashboard.csv"))
    if charts:
        path = os.path.join(args.out, "demo_topology.png")
        visualize.plot_topology(result.sim, result.first_path, result.second_path,
                                result.killed, path)
        print(f"  chart: {path}")


def run_failures(args, radio, charts):
    rows = scenarios.failures(trials=args.trials, seed=args.seed, radio=radio)
    print_table(rows, f"FAILURE TEST ({args.trials} random networks per row)")
    save_csv(rows, os.path.join(args.out, "failures.csv"))
    if charts:
        path = os.path.join(args.out, "failures.png")
        visualize.plot_failures(rows, path)
        print(f"  chart: {path}")


def run_load(args, radio, charts):
    trials = max(1, args.trials // 4)
    rows = scenarios.load(trials=trials, seed=args.seed, radio=radio)
    print_table(rows, f"LOAD TEST ({trials} random networks per row)")
    save_csv(rows, os.path.join(args.out, "load.csv"))
    if charts:
        path = os.path.join(args.out, "load.png")
        visualize.plot_load(rows, path)
        print(f"  chart: {path}")


def run_health(args, radio, charts):
    result = scenarios.health(trials=max(1, args.trials // 2), seed=args.seed, radio=radio)
    print_table([result], "HEALTH TEST (heartbeats every 15 min, alert after 30 min of silence)")
    save_csv([result], os.path.join(args.out, "health.csv"))


def run_sensitivity(args, radio, charts):
    rows = scenarios.sensitivity(trials=args.trials, seed=args.seed)
    print_table(rows, f"SENSITIVITY TEST: failure test re-run with other radio assumptions "
                      f"({args.trials} random networks per row)")
    save_csv(rows, os.path.join(args.out, "sensitivity.csv"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Emergency mesh network simulator")
    parser.add_argument("scenario", choices=["demo", "failures", "load", "health", "sensitivity", "all"])
    parser.add_argument("--trials", type=int, default=20, help="random networks per data point")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--range", type=float, default=800.0, dest="range_m",
                        help="radio range in metres (calibrate from field tests)")
    parser.add_argument("--sf", type=int, default=9, help="LoRa spreading factor (7-12)")
    parser.add_argument("--loss", type=float, default=0.02, help="random packet loss (0-1)")
    parser.add_argument("--out", default="results", help="folder for CSV files and charts")
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    radio = RadioConfig(spreading_factor=args.sf, range_m=args.range_m,
                        loss_probability=args.loss)
    charts = not args.no_charts and visualize.matplotlib_available()
    if not args.no_charts and not charts:
        print("matplotlib not installed: skipping charts (pip install -r requirements.txt)")

    print(f"Radio model: range {radio.range_m:.0f} m, SF{radio.spreading_factor}, "
          f"{radio.loss_probability:.0%} random loss. Not yet calibrated with field tests.")
    runners = {"demo": run_demo, "failures": run_failures, "load": run_load,
               "health": run_health, "sensitivity": run_sensitivity}
    selected = runners if args.scenario == "all" else {args.scenario: runners[args.scenario]}
    for run in selected.values():
        run(args, radio, charts)


if __name__ == "__main__":
    main()
