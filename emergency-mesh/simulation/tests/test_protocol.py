import unittest

from meshsim.protocol import (MAX_SOS_BYTES, EmergencyType, Flag, Packet, PacketType, Priority,
                              decode, encode, lora_airtime)


class ProtocolTests(unittest.TestCase):
    def test_sos_round_trip(self):
        p = Packet(type=PacketType.SOS, origin=12, seq=345, attempt=2, hop_limit=5, hop_count=1,
                   priority=Priority.CRITICAL, emergency_type=EmergencyType.TRAPPED, people=4,
                   flags=int(Flag.INJURED | Flag.CHILD), session=777, landmark="Blue gate, Ln 3")
        self.assertEqual(decode(encode(p)), p)

    def test_ack_and_heartbeat_round_trip(self):
        ack = Packet(type=PacketType.ACK, origin=0, seq=9, ref_origin=12, ref_seq=345)
        hb = Packet(type=PacketType.HEARTBEAT, origin=3, seq=1, battery_mv=3850,
                    uptime_s=86400, neighbours=4)
        self.assertEqual(decode(encode(ack)), ack)
        self.assertEqual(decode(encode(hb)), hb)

    def test_landmark_truncated_to_16_bytes(self):
        p = Packet(type=PacketType.SOS, origin=1, seq=1, landmark="x" * 40)
        self.assertEqual(len(decode(encode(p)).landmark.encode()), 16)
        self.assertLessEqual(len(encode(p)), MAX_SOS_BYTES)
        hindi = Packet(type=PacketType.SOS, origin=1, seq=1, landmark="स्कूल की छत पर")
        decode(encode(hindi))  # must not split a character into invalid bytes

    def test_max_sos_size_is_31_bytes(self):
        self.assertEqual(MAX_SOS_BYTES, 31)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            decode(b"\x01\x02")
        with self.assertRaises(ValueError):
            decode(bytes([99]) + bytes(8))
        with self.assertRaises(ValueError):
            encode(Packet(type=PacketType.SOS, origin=1, seq=1, people=300))

    def test_airtime(self):
        # SF9 / 125 kHz / CR 4/5 / 31-byte payload is about 247 ms.
        self.assertAlmostEqual(lora_airtime(31, 9), 0.2468, places=3)
        self.assertLess(lora_airtime(31, 7), lora_airtime(31, 9))
        self.assertLess(lora_airtime(31, 9), lora_airtime(31, 12))


if __name__ == "__main__":
    unittest.main()
