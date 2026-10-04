import unittest

from meshsim import (EmergencyType, Flag, Node, NodeConfig, Packet, PacketType, Priority,
                     RadioConfig, Simulator)

PERFECT_RADIO = RadioConfig(range_m=600, loss_probability=0.0)


def line(n, config=None):
    """Base (id 0) then nodes 1..n-1 in a straight line, 500 m apart: only neighbours hear each other."""
    return [Node(i, i * 500.0, 0.0, is_base=(i == 0), config=config) for i in range(n)]


def sos(node, priority=Priority.CRITICAL, session=1):
    return node.originate_sos(priority, EmergencyType.TRAPPED, people=2,
                              flags=Flag.INJURED, landmark="test", session=session)


class MeshTests(unittest.TestCase):
    def test_multi_hop_delivery_and_ack(self):
        sim = Simulator(line(4), PERFECT_RADIO, seed=1)
        pkt = sos(sim.nodes[3])
        sim.run(60)
        incident = sim.nodes[0].incidents[pkt.incident_key]
        self.assertEqual(incident.hops, 3)
        self.assertEqual(incident.path, (3, 2, 1, 0))
        self.assertIsNotNone(sim.nodes[3].sent_sos[pkt.seq].acked_at)

    def test_duplicates_are_dropped(self):
        sim = Simulator(line(4), PERFECT_RADIO, seed=2)
        sos(sim.nodes[3])
        sim.run(60)
        self.assertGreater(sim.stats["duplicates_dropped"], 0)
        self.assertEqual(len(sim.nodes[0].incidents), 1)

    def test_hop_limit_stops_far_messages(self):
        cfg = NodeConfig(hop_limit=2, max_attempts=2, ack_timeout_s=30)
        sim = Simulator(line(4, cfg), PERFECT_RADIO, seed=3)
        sos(sim.nodes[3])
        sim.run(600)
        self.assertEqual(sim.nodes[0].incidents, {})
        self.assertGreater(sim.stats["hop_limit_drops"], 0)
        self.assertEqual(sim.stats["sos_gave_up"], 1)

    def test_reroutes_around_dead_relay(self):
        # Diamond: 3 can reach base 0 via relay 1 or relay 2.
        nodes = [Node(0, 0, 0, is_base=True), Node(1, 400, 300), Node(2, 400, -300),
                 Node(3, 800, 0)]
        sim = Simulator(nodes, PERFECT_RADIO, seed=4)
        sim.kill(1)
        pkt = sos(sim.nodes[3])
        sim.run(60)
        self.assertEqual(sim.nodes[0].incidents[pkt.incident_key].path, (3, 2, 0))

    def test_retry_recovers_after_relay_returns(self):
        # Middle relay is dead when the SOS is sent, so the first attempt is lost;
        # a replacement relay is "installed" and a retry gets through.
        nodes = [Node(0, 0, 0, is_base=True), Node(1, 500, 0), Node(2, 1000, 0)]
        sim = Simulator(nodes, PERFECT_RADIO, seed=5)
        sim.kill(1)
        pkt = sos(sim.nodes[2])
        sim.run(30)
        self.assertEqual(sim.nodes[0].incidents, {})
        sim.nodes[1].alive = True
        sim.run(400)
        incident = sim.nodes[0].incidents[pkt.incident_key]
        self.assertGreater(incident.attempt, 1)
        self.assertGreater(sim.stats["retries"], 0)

    def test_priority_queue_sends_critical_first(self):
        sim = Simulator([Node(0, 0, 0, is_base=True), Node(1, 500, 0)], PERFECT_RADIO, seed=6)
        node = sim.nodes[1]
        node._tx_scheduled = True  # hold transmissions so the queue fills up
        for prio, session in [(Priority.SAFE, 1), (Priority.SUPPLIES, 2),
                              (Priority.CRITICAL, 3), (Priority.URGENT, 4)]:
            sos(node, prio, session)
        self.assertEqual(node.outbox_priorities(), [3, 2, 1, 0])

    def test_rate_limit_per_session(self):
        sim = Simulator(line(2), PERFECT_RADIO, seed=7)
        node = sim.nodes[1]
        results = [sos(node, session=42) for _ in range(4)]
        self.assertEqual(sum(r is not None for r in results), 3)
        self.assertIsNotNone(sos(node, session=43))  # a different phone is unaffected
        self.assertEqual(sim.stats["rate_limited"], 1)

    def test_collision_destroys_both_packets(self):
        nodes = [Node(0, 0, 0, is_base=True), Node(1, 500, 0), Node(2, -500, 0)]
        sim = Simulator(nodes, PERFECT_RADIO, seed=8)
        a = Packet(type=PacketType.HEARTBEAT, origin=1, seq=1)
        b = Packet(type=PacketType.HEARTBEAT, origin=2, seq=1)
        sim.transmit(sim.nodes[1], a)
        sim.transmit(sim.nodes[2], b)  # same instant: overlap at the base
        sim.run(5)
        self.assertEqual(sim.stats["collisions"], 1)
        self.assertEqual(sim.nodes[0].last_heartbeat, {})

    def test_heartbeats_detect_dead_node(self):
        cfg = NodeConfig(heartbeat_interval_s=60, silent_alert_s=150)
        sim = Simulator(line(3, cfg), PERFECT_RADIO, seed=9)
        for n in (1, 2):
            sim.nodes[n].start_heartbeats(1)
        sim.run(200)
        self.assertEqual(sim.nodes[0].silent_nodes([1, 2]), [])
        sim.kill(2)
        sim.run(500)
        self.assertEqual(sim.nodes[0].silent_nodes([1, 2]), [2])

    def test_reachability_helper(self):
        sim = Simulator(line(4), PERFECT_RADIO, seed=10)
        self.assertTrue(sim.reachable_within(3, 0, 3))
        self.assertFalse(sim.reachable_within(3, 0, 2))
        sim.kill(2)
        self.assertFalse(sim.reachable_within(3, 0, 6))


if __name__ == "__main__":
    unittest.main()
