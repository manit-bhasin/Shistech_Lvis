"""Behaviour of one mesh node. The same rules are intended for the ESP32 firmware."""
from __future__ import annotations

import heapq
import itertools
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

from .protocol import DEFAULT_HOP_LIMIT, Flag, Packet, PacketType, Priority

if TYPE_CHECKING:
    from .network import Simulator


@dataclass
class NodeConfig:
    hop_limit: int = DEFAULT_HOP_LIMIT
    seen_cache_size: int = 256          # recent message IDs remembered for dedup
    ack_timeout_s: float = 90.0         # wait this long for an ACK before retrying
    max_attempts: int = 5               # total sends of one SOS (1 + 4 retries)
    rate_limit_count: int = 3           # SOS per sender ID (home unit) ...
    rate_limit_window_s: float = 600.0  # ... per this many seconds
    heartbeat_interval_s: float = 900.0
    silent_alert_s: float = 1800.0      # base flags a node silent for this long
    jitter_max_s: float = 1.0           # random wait before (re)broadcasting
    busy_backoff_s: tuple = (0.2, 1.5)  # wait range when the channel is busy


@dataclass
class OutgoingSOS:
    packet: Packet
    created_at: float
    attempts: int = 1
    acked_at: Optional[float] = None


@dataclass
class Incident:
    """One emergency as seen on the rescue dashboard."""
    origin: int
    seq: int
    priority: Priority
    emergency_type: int
    people: int
    flags: int
    landmark: str
    created_at: float
    received_at: float
    hops: int
    attempt: int
    path: tuple

    @property
    def latency_s(self) -> float:
        return self.received_at - self.created_at


class Node:
    def __init__(self, node_id: int, x: float, y: float, is_base: bool = False,
                 config: Optional[NodeConfig] = None, battery_mv: int = 3900):
        self.id = node_id
        self.x, self.y = x, y
        self.is_base = is_base
        self.config = config or NodeConfig()
        self.battery_mv = battery_mv
        self.sim: Optional[Simulator] = None
        self.alive = True
        self.boot_time = 0.0
        # radio state (managed by the simulator)
        self.tx_until = 0.0
        self.active_rx: dict = {}
        # forwarding state
        self._seq = itertools.count(1)
        self._seen: set = set()
        self._seen_order: deque = deque()
        self._outbox: list = []
        self._outbox_counter = itertools.count()
        self._tx_scheduled = False
        self._session_log: dict = {}
        # SOS messages this node created
        self.sent_sos: dict = {}
        # base station only
        self.incidents: dict = {}
        self.last_heartbeat: dict = {}

    # ------------------------------------------------------------------ helpers
    def attach(self, sim: "Simulator") -> None:
        self.sim = sim
        self.boot_time = sim.now

    @property
    def now(self) -> float:
        return self.sim.now

    def _next_seq(self) -> int:
        return next(self._seq) & 0xFFFF

    def _remember(self, key: tuple) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > self.config.seen_cache_size:
            self._seen.discard(self._seen_order.popleft())

    def channel_busy(self) -> bool:
        return self.tx_until > self.now or bool(self.active_rx)

    def kill(self) -> None:
        self.alive = False
        self._outbox.clear()
        self.active_rx.clear()

    # ------------------------------------------------------------ sending SOS
    def originate_sos(self, priority: Priority, emergency_type: int, people: int = 1,
                      flags: int = Flag.NONE, landmark: str = "",
                      session: int = 0) -> Optional[Packet]:
        """Called when someone submits the SOS form or presses the node's button."""
        if not self.alive:
            return None
        if not self._rate_ok(session):
            self.sim.stats["rate_limited"] += 1
            return None
        pkt = Packet(type=PacketType.SOS, origin=self.id, seq=self._next_seq(),
                     hop_limit=self.config.hop_limit, priority=Priority(priority),
                     emergency_type=int(emergency_type), people=people, flags=int(flags),
                     session=session, landmark=landmark, path=(self.id,),
                     created_at=self.now)
        self.sent_sos[pkt.seq] = OutgoingSOS(pkt, self.now)
        self.sim.stats["sos_created"] += 1
        if self.is_base:  # an SOS raised at the base station itself
            self._base_receive(pkt, hops=0)
            self.sent_sos[pkt.seq].acked_at = self.now
            return pkt
        self._remember(pkt.msg_key)
        self._enqueue(pkt)
        self.sim.schedule(self.now + self.config.ack_timeout_s, self._check_ack, pkt.seq)
        return pkt

    def _rate_ok(self, session: int) -> bool:
        log = self._session_log.setdefault(session, deque())
        while log and self.now - log[0] >= self.config.rate_limit_window_s:
            log.popleft()
        if len(log) >= self.config.rate_limit_count:
            return False
        log.append(self.now)
        return True

    def _check_ack(self, seq: int) -> None:
        out = self.sent_sos[seq]
        if out.acked_at is not None or not self.alive:
            return
        if out.attempts >= self.config.max_attempts:
            self.sim.stats["sos_gave_up"] += 1
            return
        out.attempts += 1
        retry = replace(out.packet, attempt=out.attempts, hop_count=0,
                        hop_limit=self.config.hop_limit, path=(self.id,))
        self.sim.stats["retries"] += 1
        self._remember(retry.msg_key)
        self._enqueue(retry)
        # exponential backoff with a little randomness
        wait = self.config.ack_timeout_s * 2 ** (out.attempts - 1)
        wait += self.sim.rng.uniform(0, self.config.ack_timeout_s * 0.25)
        self.sim.schedule(self.now + wait, self._check_ack, seq)

    # ------------------------------------------------------------- heartbeats
    def start_heartbeats(self, first_after_s: float) -> None:
        self.sim.schedule(self.now + first_after_s, self._heartbeat)

    def _heartbeat(self) -> None:
        if not self.alive:
            return
        pkt = Packet(type=PacketType.HEARTBEAT, origin=self.id, seq=self._next_seq(),
                     hop_limit=self.config.hop_limit, priority=Priority.SAFE,
                     battery_mv=self.battery_mv,
                     uptime_s=int(self.now - self.boot_time),
                     neighbours=len(self.sim.neighbours(self.id)),
                     path=(self.id,), created_at=self.now)
        self._remember(pkt.msg_key)
        self._enqueue(pkt)
        self.sim.schedule(self.now + self.config.heartbeat_interval_s, self._heartbeat)

    def silent_nodes(self, node_ids, now: Optional[float] = None) -> list:
        """Base station: nodes whose last heartbeat is older than the alert threshold."""
        now = self.now if now is None else now
        limit = self.config.silent_alert_s
        return [n for n in node_ids
                if n != self.id and now - self.last_heartbeat.get(n, self.boot_time) > limit]

    # --------------------------------------------------------------- receiving
    def on_receive(self, pkt: Packet) -> None:
        if not self.alive:
            return
        if pkt.msg_key in self._seen:
            self.sim.stats["duplicates_dropped"] += 1
            return
        self._remember(pkt.msg_key)
        hops = pkt.hop_count + 1

        if self.is_base:
            self._base_receive(pkt, hops)
            return

        if pkt.type == PacketType.ACK and pkt.ref_origin == self.id:
            out = self.sent_sos.get(pkt.ref_seq)
            if out is not None and out.acked_at is None:
                out.acked_at = self.now
            return

        if pkt.hop_limit > 1:
            self.sim.stats["forwards"] += 1
            self._enqueue(replace(pkt, hop_limit=pkt.hop_limit - 1, hop_count=hops,
                                  path=pkt.path + (self.id,)))
        else:
            self.sim.stats["hop_limit_drops"] += 1

    def _base_receive(self, pkt: Packet, hops: int) -> None:
        if pkt.type == PacketType.HEARTBEAT:
            self.last_heartbeat[pkt.origin] = self.now
            return
        if pkt.type != PacketType.SOS:
            return  # the base ignores ACK echoes
        if pkt.incident_key not in self.incidents:
            self.incidents[pkt.incident_key] = Incident(
                origin=pkt.origin, seq=pkt.seq, priority=pkt.priority,
                emergency_type=pkt.emergency_type, people=pkt.people, flags=pkt.flags,
                landmark=pkt.landmark, created_at=pkt.created_at, received_at=self.now,
                hops=hops, attempt=pkt.attempt, path=pkt.path + (self.id,))
        if pkt.origin == self.id:
            return
        # Acknowledge every new attempt, in case an earlier ACK was lost.
        ack = Packet(type=PacketType.ACK, origin=self.id, seq=self._next_seq(),
                     hop_limit=self.config.hop_limit, priority=pkt.priority,
                     ref_origin=pkt.origin, ref_seq=pkt.seq, path=(self.id,),
                     created_at=self.now)
        self._remember(ack.msg_key)
        self._enqueue(ack)

    # ------------------------------------------------------------ transmitting
    def _enqueue(self, pkt: Packet) -> None:
        # Highest priority first; equal priority in arrival order.
        heapq.heappush(self._outbox, (-int(pkt.priority), self.now,
                                      next(self._outbox_counter), pkt))
        self._schedule_tx()

    def _schedule_tx(self) -> None:
        if self._tx_scheduled or not self._outbox:
            return
        self._tx_scheduled = True
        delay = self.sim.rng.uniform(0, self.config.jitter_max_s)
        self.sim.schedule(self.now + delay, self._try_tx)

    def _try_tx(self) -> None:
        self._tx_scheduled = False
        if not self.alive or not self._outbox:
            return
        if self.channel_busy():  # listen before talk
            self._tx_scheduled = True
            self.sim.schedule(self.now + self.sim.rng.uniform(*self.config.busy_backoff_s),
                              self._try_tx)
            return
        _, _, _, pkt = heapq.heappop(self._outbox)
        self.sim.transmit(self, pkt)

    def on_tx_done(self) -> None:
        if self.alive:
            self._schedule_tx()

    def outbox_priorities(self) -> list:
        """Priorities in the order they would be sent (used by tests)."""
        return [int(item[3].priority) for item in sorted(self._outbox)]
