// Engine tests. Run with `node --test` from inside web_demo/ (Node 20 or newer).
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Mesh = require("../mesh.js");

const SEED = 20251004;
const REPORT = { priority: 3, emergencyType: 2, people: 3, landmark: "School rooftop" };

function near(actual, expected, places) {
  assert.ok(Math.abs(actual - expected) < 0.5 * 10 ** -places,
    `expected ${actual} to be ${expected} to ${places} decimal places`);
}

// A person 50 m east of the node sends an SOS.
function sosNear(sim, id, report) {
  const node = sim.nodes[id];
  return Mesh.sendSOS(sim, node.x + 50, node.y, report || REPORT);
}

function toggle(sim, id) {
  const node = sim.nodes[id];
  return Mesh.toggleNode(sim, node.x, node.y);
}

function destroy(sim, ...ids) {
  for (const id of ids) {
    toggle(sim, id);
    assert.equal(sim.nodes[id].alive, false);
  }
}

const lastMessage = (sim) => sim.messages[sim.messages.length - 1].text;

test("1. LoRa airtime matches protocol.py", () => {
  near(Mesh.airtime(31), 0.2468, 3);
  near(Mesh.airtime(13), 0.1649, 3);
});

test("2. landmark16 keeps at most 16 bytes and never splits a character", () => {
  const bytes = (s) => new TextEncoder().encode(s).length;
  assert.equal(Mesh.landmark16("x".repeat(40)), "x".repeat(16));

  const hindi = "स्कूल की छत पर";
  const cut = Mesh.landmark16(hindi);
  assert.ok(bytes(cut) <= 16);
  assert.ok(hindi.startsWith(cut));
  assert.ok(!cut.includes("�"));

  // 2 + 15 bytes, so the 16-byte cut lands inside the last character, which is dropped.
  const split = Mesh.landmark16("abस्कूल");
  assert.equal(split, "abस्कू");
  assert.equal(bytes(split), 14);
});

test("3. toLatLon of node 1", () => {
  const n1 = Mesh.LAYOUT[1];
  assert.deepEqual(Mesh.toLatLon(n1[1], n1[2]), { lat: 12.98104, lon: 80.21713 });
});

test("4. neighbours within 800 m", () => {
  const sim = Mesh.createSim({ seed: SEED });
  assert.deepEqual([...sim.nodes[0].neighbours].sort((a, b) => a - b), [8, 12, 13, 17]);
  assert.deepEqual([...sim.nodes[5].neighbours].sort((a, b) => a - b), [4, 9, 10]);
});

test("5. an SOS near node 1 reaches the base and the sender is confirmed", () => {
  const sim = Mesh.createSim({ seed: SEED });
  const sos = sosNear(sim, 1);
  assert.equal(sos.origin, 1);
  Mesh.run(sim, 60);
  const incident = sim.incidents.get(sos.key);
  assert.ok(incident, "incident recorded at the base");
  assert.equal(incident.path[0], 1);
  assert.equal(incident.path[incident.path.length - 1], 0);
  assert.ok(incident.hops >= 4 && incident.hops <= 6, `hops was ${incident.hops}`);
  assert.equal(sos.status, "confirmed");
});

test("6. the SOS routes around a destroyed relay", () => {
  const sim = Mesh.createSim({ seed: SEED });
  destroy(sim, 7);
  const sos = sosNear(sim, 1);
  Mesh.run(sim, 60);
  const incident = sim.incidents.get(sos.key);
  assert.ok(incident, "delivered");
  assert.ok(!incident.path.includes(7), `path was ${incident.path}`);
});

test("7. a node with no working neighbours gives up after 5 sends", () => {
  const sim = Mesh.createSim({ seed: SEED });
  destroy(sim, 4, 9, 10);
  const sos = sosNear(sim, 5);
  assert.equal(sos.origin, 5);
  Mesh.run(sim, 3000);
  assert.equal(sim.incidents.size, 0);
  assert.equal(sos.status, "failed");
  assert.equal(sim.stats.transmissions, 5);
  assert.equal(sim.stats.gaveUp, 1);
});

test("8. a retry gets through once a neighbour is repaired", () => {
  const sim = Mesh.createSim({ seed: SEED });
  destroy(sim, 4, 9, 10);
  const sos = sosNear(sim, 5);
  Mesh.run(sim, 120);
  toggle(sim, 10);
  assert.equal(sim.nodes[10].alive, true);
  Mesh.run(sim, 3000);
  const incident = sim.incidents.get(sos.key);
  assert.ok(incident, "delivered");
  assert.ok(incident.attempt >= 2, `attempt was ${incident.attempt}`);
});

test("9. the outbox sends the highest priority first", () => {
  const sim = Mesh.createSim({ seed: SEED });
  sim.nodes[1].txScheduled = true;  // hold transmissions so the queue fills up
  for (const priority of [0, 1, 3, 2]) sosNear(sim, 1, { ...REPORT, priority });
  assert.deepEqual(sim.nodes[1].outbox.map((p) => p.priority), [3, 2, 1, 0]);
});

test("10. a click out of home unit reach is a miss", () => {
  const sim = Mesh.createSim({ seed: SEED });
  const distance = Mesh.nearestNode(sim, 300, 300, Infinity, true).distance;
  assert.ok(distance > 316 && distance < 318, `nearest node was ${distance} m away`);
  assert.equal(Mesh.sendSOS(sim, 300, 300, REPORT), null);
  assert.equal(sim.misses.length, 1);
  assert.equal(sim.stats.sosSent, 0);
  assert.equal(lastMessage(sim),
    "No working relay within 200 m of that spot (home unit reach, assumed, not measured). Move closer to one.");
});

test("11. the base cannot be destroyed", () => {
  const sim = Mesh.createSim({ seed: SEED });
  toggle(sim, 0);
  assert.equal(sim.nodes[0].alive, true);
  assert.equal(lastMessage(sim), "The base station stays up in this demo.");
});

test("12. duplicates are dropped and the base records one incident", () => {
  const sim = Mesh.createSim({ seed: SEED });
  sosNear(sim, 1);
  Mesh.run(sim, 60);
  assert.ok(sim.stats.duplicatesDropped > 0);
  assert.equal(sim.incidents.size, 1);
});

test("13. a hop limit of 2 never reaches the base 4 hops away", () => {
  const sim = Mesh.createSim({ seed: SEED, config: { hopLimit: 2 } });
  const sos = sosNear(sim, 1);
  Mesh.run(sim, 3000);
  assert.equal(sim.incidents.size, 0);
  assert.equal(sos.status, "failed");
  assert.ok(sim.stats.hopLimitDrops > 0);
});

test("14. seen-message memory stays at 256 entries per node after crowds", () => {
  const sim = Mesh.createSim({ seed: SEED });
  // One crowd adds about 40 message IDs per node; ten crowds push past the limit.
  for (let i = 0; i < 10; i++) Mesh.crowd(sim);
  Mesh.run(sim, 3000);
  const sizes = sim.nodes.map((n) => n.seen.size);
  for (const node of sim.nodes) {
    assert.ok(node.seen.size <= 256, `node ${node.id} remembers ${node.seen.size}`);
    assert.equal(node.seenOrder.length, node.seen.size);
  }
  assert.equal(Math.max(...sizes), 256, "the limit was reached, so old IDs were forgotten");
});

test("15. the same seed and actions give identical results", () => {
  function play() {
    const sim = Mesh.createSim({ seed: SEED });
    sosNear(sim, 1);
    destroy(sim, 7);
    Mesh.crowd(sim);
    Mesh.run(sim, 600);
    return JSON.stringify({
      incidents: [...sim.incidents.values()],
      sos: [...sim.sos.values()],
      stats: sim.stats,
      transmissions: sim.transmissions,
      messages: sim.messages,
    });
  }
  assert.equal(play(), play());
});

// Extra checks of rules stated in the spec.

test("packet sizes match protocol.py", () => {
  assert.equal(Mesh.packetSize({ type: "SOS", landmark: "" }), 15);
  assert.equal(Mesh.packetSize({ type: "SOS", landmark: "x".repeat(16) }), 31);
  assert.equal(Mesh.packetSize({ type: "ACK" }), 13);
});

test("sends happen at about 0, 90, 270, 630 and 1350 s", () => {
  const sim = Mesh.createSim({ seed: SEED });
  destroy(sim, 4, 9, 10);
  sosNear(sim, 5);
  Mesh.run(sim, 3000);
  const starts = sim.transmissions.map((t) => t.start);
  const nominal = [0, 90, 270, 630, 1350];
  assert.equal(starts.length, 5);
  starts.forEach((start, k) => {
    // up to 1 s random wait per send, plus up to 22.5 s per earlier doubling wait
    const slack = 1 + 22.5 * Math.max(0, k - 1);
    assert.ok(start >= nominal[k] && start <= nominal[k] + slack, `send ${k + 1} at ${start} s`);
  });
});

test("an SOS raised at the base is recorded at once with 0 hops and confirmed", () => {
  const sim = Mesh.createSim({ seed: SEED });
  const sos = sosNear(sim, 0);
  assert.equal(sos.origin, 0);
  const incident = sim.incidents.get(sos.key);
  assert.ok(incident);
  assert.equal(incident.hops, 0);
  assert.equal(sos.status, "confirmed");
  Mesh.run(sim, 10);
  assert.equal(sim.stats.transmissions, 0);
});

test("neighbours still receive a packet if the sender is destroyed mid-transmission", () => {
  const sim = Mesh.createSim({ seed: SEED });
  const sos = sosNear(sim, 1);
  Mesh.run(sim, sim.queue[0].t);  // node 1 starts transmitting
  assert.equal(sim.transmissions.length, 1);
  destroy(sim, 1);
  Mesh.run(sim, 60);
  assert.ok(sim.incidents.has(sos.key), "nodes 2 and 6 received and forwarded it");
});
