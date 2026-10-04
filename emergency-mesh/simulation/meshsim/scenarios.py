"""Disaster scenarios run on the simulator."""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

from .network import RadioConfig, Simulator
from .node import Node, NodeConfig
from .protocol import EmergencyType, Flag, Priority

# Sample reference point (Velachery, Chennai) used only to show map coordinates.
REFERENCE_LATLON = (12.9815, 80.2180)

LANDMARKS = ["School rooftop", "Near temple", "Opp. bus stop", "Red bldg, 2nd fl",
             "Blue gate, Ln 3", "Community hall", "Water tank", "Ration shop"]

PRIORITY_MIX = [(Priority.CRITICAL, 0.3), (Priority.URGENT, 0.3),
                (Priority.SUPPLIES, 0.2), (Priority.SAFE, 0.2)]

TYPE_FOR_PRIORITY = {
    Priority.CRITICAL: [EmergencyType.MEDICAL, EmergencyType.TRAPPED, EmergencyType.COLLAPSE],
    Priority.URGENT: [EmergencyType.FLOODING, EmergencyType.FIRE],
    Priority.SUPPLIES: [EmergencyType.OTHER],
    Priority.SAFE: [EmergencyType.OTHER],
}


def to_latlon(x: float, y: float) -> tuple:
    lat0, lon0 = REFERENCE_LATLON
    lat = lat0 + y / 111_320
    lon = lon0 + x / (111_320 * math.cos(math.radians(lat0)))
    return round(lat, 5), round(lon, 5)


def grid_nodes(rng: random.Random, rows: int = 5, cols: int = 5, spacing_m: float = 600.0,
               jitter_m: float = 100.0, config: NodeConfig | None = None) -> list:
    """Nodes on a jittered grid (like gathering points across a neighbourhood).
    The base station (id 0) sits at the centre."""
    centre = (rows // 2, cols // 2)
    nodes, next_id = [], 1
    for r in range(rows):
        for c in range(cols):
            if (r, c) == centre:
                nodes.append(Node(0, c * spacing_m, r * spacing_m, is_base=True, config=config))
                continue
            x = c * spacing_m + rng.uniform(-jitter_m, jitter_m)
            y = r * spacing_m + rng.uniform(-jitter_m, jitter_m)
            nodes.append(Node(next_id, x, y, config=config))
            next_id += 1
    return sorted(nodes, key=lambda n: n.id)


def random_sos(node: Node, rng: random.Random, priority: Priority | None = None):
    if priority is None:
        roll, priority = rng.random(), Priority.SAFE
        for prio, share in PRIORITY_MIX:
            if roll < share:
                priority = prio
                break
            roll -= share
    flags = Flag.NONE
    if priority == Priority.CRITICAL:
        flags |= rng.choice([Flag.INJURED, Flag.TRAPPED, Flag.INJURED | Flag.TRAPPED])
    if priority == Priority.URGENT:
        flags |= rng.choice([Flag.WATER_RISING, Flag.CHILD, Flag.ELDERLY])
    return node.originate_sos(priority, rng.choice(TYPE_FOR_PRIORITY[priority]),
                              people=rng.randint(1, 8), flags=flags,
                              landmark=rng.choice(LANDMARKS), session=rng.randrange(1, 65535))


def _percentile(values: list, pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[k]


def _median(values: list) -> float:
    return statistics.median(values) if values else float("nan")


# ---------------------------------------------------------------------- demo
@dataclass
class DemoResult:
    sim: Simulator
    first_path: tuple
    second_path: tuple
    killed: int
    origin: int


def demo(seed: int = 7, radio: RadioConfig | None = None) -> DemoResult:
    """A critical SOS crosses the network; the relay it used is then destroyed and a
    second SOS from the same place finds another route."""
    rng = random.Random(f"demo-{seed}")
    nodes = grid_nodes(rng)
    sim = Simulator(nodes, radio, seed=seed)
    base = sim.nodes[0]
    origin = max(nodes, key=lambda n: math.dist((n.x, n.y), (base.x, base.y)))

    first = origin.originate_sos(Priority.CRITICAL, EmergencyType.TRAPPED, people=4,
                                 flags=Flag.INJURED | Flag.TRAPPED,
                                 landmark="School rooftop", session=101)
    others = [n for n in nodes if not n.is_base and n is not origin]
    for node in rng.sample(others, 4):
        sim.schedule(rng.uniform(1, 20), random_sos, node, rng)
    sim.run(120)
    first_path = base.incidents[first.incident_key].path

    killed = first_path[len(first_path) // 2]  # a relay in the middle of the route
    sim.kill(killed)
    second = origin.originate_sos(Priority.URGENT, EmergencyType.FLOODING, people=6,
                                  flags=Flag.WATER_RISING | Flag.ELDERLY,
                                  landmark="School rooftop", session=102)
    sim.run(400)
    incident = base.incidents.get(second.incident_key)
    second_path = incident.path if incident else ()
    return DemoResult(sim, first_path, second_path, killed, origin.id)


def dashboard_rows(sim: Simulator) -> list:
    base = sim.nodes[0]
    rows = []
    for inc in sorted(base.incidents.values(), key=lambda i: (-int(i.priority), i.received_at)):
        node = sim.nodes[inc.origin]
        lat, lon = to_latlon(node.x, node.y)
        rows.append({
            "priority": Priority(inc.priority).name,
            "type": EmergencyType(inc.emergency_type).name,
            "node": inc.origin,
            "lat": lat, "lon": lon,
            "people": inc.people,
            "flags": "|".join(f.name for f in Flag if f and inc.flags & f) or "-",
            "landmark": inc.landmark,
            "hops": inc.hops,
            "delay_s": round(inc.latency_s, 1),
        })
    return rows


# ------------------------------------------------------------------ failures
def failures(trials: int = 20, fractions=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5), seed: int = 1,
             radio: RadioConfig | None = None, duration_s: float = 1800) -> list:
    """Destroy a random share of relay nodes, then every surviving node sends one SOS."""
    results = []
    for frac in fractions:
        sent = delivered = acked = reachable = 0
        latencies, hops, transmissions = [], [], []
        for t in range(trials):
            rng = random.Random(f"fail-{seed}-{frac}-{t}")
            nodes = grid_nodes(rng)
            sim = Simulator(nodes, radio, seed=rng.randrange(1 << 30))
            others = [n for n in nodes if not n.is_base]
            for dead in rng.sample(others, round(frac * len(others))):
                sim.kill(dead.id)
            alive = [n for n in others if n.alive]
            for node in alive:
                sim.schedule(rng.uniform(0, 30), random_sos, node, rng)
            sim.run(duration_s)

            base = sim.nodes[0]
            hop_limit = nodes[0].config.hop_limit
            sent += len(alive)
            reachable += sum(sim.reachable_within(n.id, 0, hop_limit) for n in alive)
            delivered += len(base.incidents)
            acked += sum(1 for n in alive for o in n.sent_sos.values() if o.acked_at is not None)
            latencies += [i.latency_s for i in base.incidents.values()]
            hops += [i.hops for i in base.incidents.values()]
            transmissions.append(sim.stats["transmissions"] / max(1, len(alive)))
        results.append({
            "failed_pct": round(frac * 100),
            "sos_sent": sent,
            "reachable_pct": round(100 * reachable / sent, 1),
            "delivered_pct": round(100 * delivered / sent, 1),
            "delivered_of_reachable_pct": (round(100 * delivered / reachable, 1)
                                           if reachable else float("nan")),
            "confirmed_pct": round(100 * acked / sent, 1),
            "median_delay_s": round(_median(latencies), 1),
            "p95_delay_s": round(_percentile(latencies, 95), 1),
            "mean_hops": round(statistics.mean(hops), 2) if hops else float("nan"),
            "tx_per_sos": round(statistics.mean(transmissions), 1),
        })
    return results


# ---------------------------------------------------------------------- load
def load(trials: int = 5, loads=(10, 50, 100, 200, 500), seed: int = 2, window_s: float = 60,
         radio: RadioConfig | None = None, duration_s: float = 3600) -> list:
    """Many people send an SOS within the same minute (no nodes destroyed)."""
    results = []
    for n_sos in loads:
        sent = delivered = within_5min = collisions = 0
        lat_by_prio = {p: [] for p in Priority}
        all_lat = []
        for t in range(trials):
            rng = random.Random(f"load-{seed}-{n_sos}-{t}")
            nodes = grid_nodes(rng)
            sim = Simulator(nodes, radio, seed=rng.randrange(1 << 30))
            others = [n for n in nodes if not n.is_base]
            for _ in range(n_sos):
                sim.schedule(rng.uniform(0, window_s), random_sos, rng.choice(others), rng)
            sim.run(duration_s)
            base = sim.nodes[0]
            for node in others:
                for out in node.sent_sos.values():
                    sent += 1
                    inc = base.incidents.get(out.packet.incident_key)
                    if inc is None:
                        continue
                    delivered += 1
                    within_5min += inc.latency_s <= 300
                    lat_by_prio[Priority(inc.priority)].append(inc.latency_s)
                    all_lat.append(inc.latency_s)
            collisions += sim.stats["collisions"]
        results.append({
            "sos_in_60s": n_sos,
            "delivered_pct": round(100 * delivered / max(1, sent), 1),
            "within_5min_pct": round(100 * within_5min / max(1, sent), 1),
            "median_delay_critical_s": round(_median(lat_by_prio[Priority.CRITICAL]), 1),
            "median_delay_urgent_s": round(_median(lat_by_prio[Priority.URGENT]), 1),
            "median_delay_supplies_s": round(_median(lat_by_prio[Priority.SUPPLIES]), 1),
            "median_delay_safe_s": round(_median(lat_by_prio[Priority.SAFE]), 1),
            "p95_delay_s": round(_percentile(all_lat, 95), 1),
            "collisions_per_sos": round(collisions / max(1, sent), 1),
        })
    return results


# -------------------------------------------------------------------- health
def health(trials: int = 10, seed: int = 3, kill_count: int = 3, kill_at_s: float = 3600,
           duration_s: float = 3 * 3600, radio: RadioConfig | None = None) -> dict:
    """Nodes send heartbeats every 15 min. Some nodes die silently; does the base notice?"""
    detected = missed = false_alarms = 0
    delays = []
    for t in range(trials):
        rng = random.Random(f"health-{seed}-{t}")
        nodes = grid_nodes(rng)
        sim = Simulator(nodes, radio, seed=rng.randrange(1 << 30))
        others = [n for n in nodes if not n.is_base]
        for node in others:
            node.start_heartbeats(rng.uniform(0, node.config.heartbeat_interval_s))
        sim.run(kill_at_s)
        dead = {n.id for n in rng.sample(others, kill_count)}
        for nid in dead:
            sim.kill(nid)
        sim.run(duration_s)
        base = sim.nodes[0]
        flagged = set(base.silent_nodes([n.id for n in others], now=duration_s))
        detected += len(flagged & dead)
        missed += len(dead - flagged)
        false_alarms += len(flagged - dead)
        for nid in flagged & dead:
            last = base.last_heartbeat.get(nid, 0.0)
            delays.append(last + base.config.silent_alert_s - kill_at_s)
    return {
        "trials": trials,
        "nodes_killed": trials * kill_count,
        "detected": detected,
        "missed": missed,
        "false_alarms": false_alarms,
        "mean_minutes_to_alert": round(statistics.mean(delays) / 60, 1) if delays else float("nan"),
        "max_minutes_to_alert": round(max(delays) / 60, 1) if delays else float("nan"),
    }


# --------------------------------------------------------------- sensitivity
def sensitivity(trials: int = 20, seed: int = 1, ranges=(500, 650, 800, 1000),
                losses=(0.0, 0.02, 0.05, 0.10), fractions=(0.0, 0.3)) -> list:
    """Re-run the failure test with different radio assumptions, to show which
    results depend on the assumed range and packet loss."""
    rows = []
    settings = [(r, 0.02) for r in ranges] + [(800, l) for l in losses if l != 0.02]
    for range_m, loss in settings:
        radio = RadioConfig(range_m=range_m, loss_probability=loss)
        for row in failures(trials=trials, fractions=fractions, seed=seed, radio=radio):
            rows.append({
                "range_m": range_m,
                "loss_pct": round(loss * 100),
                "failed_pct": row["failed_pct"],
                "reachable_pct": row["reachable_pct"],
                "delivered_pct": row["delivered_pct"],
                "delivered_of_reachable_pct": row["delivered_of_reachable_pct"],
                "median_delay_s": row["median_delay_s"],
            })
    return rows
