"""Discrete-event network simulator.

Radio model (deliberately simple, stated in the README):
- A node hears every live node within `range_m` (flat ground, no terrain).
- Two transmissions overlapping at a receiver destroy each other (collision).
- A node cannot receive while it is transmitting (half-duplex).
- Each successful reception can still be lost at random (`loss_probability`).
Time on air comes from the real LoRa formula, so congestion is realistic.
"""
from __future__ import annotations

import heapq
import itertools
import math
import random
from collections import Counter, deque
from dataclasses import dataclass
from typing import Iterable

from .node import Node
from .protocol import Packet, encode, lora_airtime


@dataclass
class RadioConfig:
    spreading_factor: int = 9
    bandwidth_hz: int = 125_000
    range_m: float = 800.0        # NOT measured yet: calibrate from field tests
    loss_probability: float = 0.02


class Simulator:
    def __init__(self, nodes: Iterable[Node], radio: RadioConfig | None = None, seed: int = 0):
        self.radio = radio or RadioConfig()
        self.rng = random.Random(seed)
        self.now = 0.0
        self.stats: Counter = Counter()
        self._queue: list = []
        self._order = itertools.count()
        self._tx_ids = itertools.count()
        self.nodes = {n.id: n for n in nodes}
        for node in self.nodes.values():
            node.attach(self)
        self._neighbours = self._compute_neighbours()

    # ------------------------------------------------------------- topology
    def _compute_neighbours(self) -> dict:
        result = {}
        for a in self.nodes.values():
            result[a.id] = [b.id for b in self.nodes.values()
                            if b.id != a.id and math.dist((a.x, a.y), (b.x, b.y)) <= self.radio.range_m]
        return result

    def neighbours(self, node_id: int) -> list:
        return self._neighbours[node_id]

    def reachable_within(self, src: int, dst: int, max_hops: int) -> bool:
        """Is there any path of live nodes from src to dst using at most max_hops hops?"""
        if not (self.nodes[src].alive and self.nodes[dst].alive):
            return False
        seen, frontier = {src}, deque([(src, 0)])
        while frontier:
            current, depth = frontier.popleft()
            if current == dst:
                return True
            if depth == max_hops:
                continue
            for nxt in self._neighbours[current]:
                if nxt not in seen and self.nodes[nxt].alive:
                    seen.add(nxt)
                    frontier.append((nxt, depth + 1))
        return False

    # ---------------------------------------------------------------- events
    def schedule(self, at: float, fn, *args) -> None:
        heapq.heappush(self._queue, (at, next(self._order), fn, args))

    def run(self, until: float) -> None:
        while self._queue and self._queue[0][0] <= until:
            at, _, fn, args = heapq.heappop(self._queue)
            self.now = at
            fn(*args)
        self.now = max(self.now, until)

    def kill(self, node_id: int) -> None:
        self.nodes[node_id].kill()
        self.stats["nodes_killed"] += 1

    # ----------------------------------------------------------------- radio
    def transmit(self, sender: Node, pkt: Packet) -> None:
        size = len(encode(pkt))
        duration = lora_airtime(size, self.radio.spreading_factor, self.radio.bandwidth_hz)
        tx_id = next(self._tx_ids)
        sender.tx_until = self.now + duration
        for key in sender.active_rx:          # half-duplex: own TX ruins receptions
            sender.active_rx[key] = True
        self.stats["transmissions"] += 1
        self.stats["bytes_sent"] += size
        self.stats["airtime_s"] += duration

        receivers = []
        for rid in self._neighbours[sender.id]:
            r = self.nodes[rid]
            if not r.alive or r.tx_until > self.now:
                continue
            corrupted = bool(r.active_rx)
            if corrupted:
                for key in r.active_rx:
                    r.active_rx[key] = True
                self.stats["collisions"] += 1
            r.active_rx[tx_id] = corrupted
            receivers.append(r)
        self.schedule(self.now + duration, self._finish_tx, sender, pkt, tx_id, receivers)

    def _finish_tx(self, sender: Node, pkt: Packet, tx_id: int, receivers: list) -> None:
        sender.on_tx_done()
        for r in receivers:
            corrupted = r.active_rx.pop(tx_id, True)
            if corrupted or not r.alive:
                continue
            if self.rng.random() < self.radio.loss_probability:
                self.stats["random_losses"] += 1
                continue
            r.on_receive(pkt)
