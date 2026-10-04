/*
 * Emergency mesh demo: simulation engine.
 *
 * Follows the forwarding rules in simulation/meshsim/node.py: duplicate dropping,
 * hop limit, highest priority sent first, a random wait before sending, a
 * confirmation (ACK) from the base, and retries with doubling waits.
 * Radio collisions, listen-before-talk and random packet loss are left out.
 *
 * No DOM code here, so the same file runs in the browser and under `node --test`.
 */
(function (root) {
  "use strict";

  // Defaults match NodeConfig, RadioConfig and protocol.py in the Python simulator.
  const DEFAULTS = Object.freeze({
    radioRange: 800,           // m: two nodes this close are neighbours
    wifiReach: 200,            // m: how far a phone reaches a node over Wi-Fi
    toggleReach: 150,          // m: how close a click must be to destroy or repair a node
    hopLimit: 6,
    seenCacheSize: 256,        // recent message IDs remembered per node
    ackTimeout: 90,            // s to wait for a confirmation before the first retry
    maxAttempts: 5,            // first send + 4 retries
    jitterMax: 1.0,            // s: random wait before sending
    busyBackoff: [0.2, 1.5],   // s: wait range while the node is still transmitting
    spreadingFactor: 9,
    bandwidthHz: 125000,
    codingRate: 1,             // 1..4 means 4/5..4/8
    preambleSymbols: 8,
    crc: true,
    explicitHeader: true,
    maxLandmarkBytes: 16,
  });

  // [id, x metres, y metres]. Node 0 is the rescue base. Same layout as the Python demo.
  const LAYOUT = Object.freeze([
    [0, 1200, 1200], [1, -94.3, -51.7], [2, 554.9, 49.8], [3, 1268.7, 62.4], [4, 1771, 56.4],
    [5, 2433.5, 53.8], [6, 94.8, 541], [7, 666.4, 571.5], [8, 1123.2, 631.2], [9, 1891.2, 604],
    [10, 2487.1, 692.6], [11, 60.1, 1234], [12, 664.4, 1212], [13, 1714, 1282.8], [14, 2470.7, 1267.3],
    [15, -36.6, 1815.8], [16, 647, 1799.7], [17, 1108.1, 1854.8], [18, 1772.6, 1895.5], [19, 2497.3, 1781.8],
    [20, 50.6, 2382.5], [21, 583.8, 2429.9], [22, 1154.9, 2311.9], [23, 1750.8, 2405.2], [24, 2408.9, 2309],
  ]);
  const BASE_ID = 0;

  const SOS = "SOS";
  const ACK = "ACK";
  const SOS_BYTES_BEFORE_LANDMARK = 15;  // 9-byte header + 6-byte SOS body
  const ACK_BYTES = 13;                  // 9-byte header + 4-byte ACK body

  const PRIORITY_LABELS = Object.freeze({ 3: "Critical", 2: "Urgent", 1: "Supplies", 0: "I'm safe" });
  // Mid-sentence form with its article: "Base received an urgent SOS ..."
  const PRIORITY_IN_SENTENCE = Object.freeze({ 3: "a critical", 2: "an urgent", 1: "a supplies", 0: "an \"I'm safe\"" });
  const EMERGENCY_TYPES = Object.freeze({
    1: "Medical", 2: "Trapped", 3: "Flooding", 4: "Fire", 5: "Building collapse", 6: "Other",
  });

  const CROWD = Object.freeze({ count: 20, windowS: 10, radiusM: 160, peopleMax: 8 });
  const CROWD_PRIORITY_MIX = [[3, 0.3], [2, 0.3], [1, 0.2], [0, 0.2]];
  const CROWD_TYPES = { 3: [1, 2, 5], 2: [3, 4], 1: [6], 0: [6] };
  const CROWD_LANDMARKS = ["Near temple", "Opp. bus stop", "Community hall", "Water tank", "Ration shop", "Blue gate, Ln 3"];

  const REF_LAT = 12.9815;  // sample location in Chennai, as in scenarios.py
  const REF_LON = 80.2180;
  const METRES_PER_DEGREE = 111320;

  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  // ------------------------------------------------------------------ helpers

  // LoRa time on air in seconds: the Semtech SX127x formula, as lora_airtime() in protocol.py.
  function airtime(bytes, config) {
    const c = config || DEFAULTS;
    const sf = c.spreadingFactor;
    const tSym = 2 ** sf / c.bandwidthHz;
    const lowDataRate = tSym > 0.016 ? 1 : 0;
    const implicitHeader = c.explicitHeader ? 0 : 1;
    const numerator = 8 * bytes - 4 * sf + 28 + 16 * (c.crc ? 1 : 0) - 20 * implicitHeader;
    const payloadSymbols = 8 + Math.max(
      Math.ceil(numerator / (4 * (sf - 2 * lowDataRate))) * (c.codingRate + 4), 0);
    return (c.preambleSymbols + 4.25) * tSym + payloadSymbols * tSym;
  }

  function packetSize(pkt) {
    return pkt.type === SOS ? SOS_BYTES_BEFORE_LANDMARK + encoder.encode(pkt.landmark).length : ACK_BYTES;
  }

  // First 16 bytes of UTF-8, never ending in half a character.
  function landmark16(text, maxBytes) {
    const limit = maxBytes === undefined ? DEFAULTS.maxLandmarkBytes : maxBytes;
    const bytes = encoder.encode(String(text));
    let end = Math.min(bytes.length, limit);
    if (end < bytes.length) {
      // A continuation byte (10xxxxxx) at the cut means a character was split: step back to its start.
      while (end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
    }
    return decoder.decode(bytes.subarray(0, end));
  }

  function toLatLon(x, y) {
    const lat = REF_LAT + y / METRES_PER_DEGREE;
    const lon = REF_LON + x / (METRES_PER_DEGREE * Math.cos(REF_LAT * Math.PI / 180));
    return { lat: Number(lat.toFixed(5)), lon: Number(lon.toFixed(5)) };
  }

  // Small seeded random number generator, so a seed always gives the same run.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function uniform(sim, low, high) {
    return low + (high - low) * sim.random();
  }

  function pick(sim, items) {
    return items[Math.floor(sim.random() * items.length)];
  }

  const msgKey = (pkt) => `${pkt.type}:${pkt.origin}:${pkt.seq}:${pkt.attempt}`;
  const incidentKey = (origin, seq) => `${origin}:${seq}`;
  const hopsText = (hops) => `${hops} hop${hops === 1 ? "" : "s"}`;

  function say(sim, text) {
    sim.messages.push({ time: sim.now, text });
  }

  // --------------------------------------------------------------- event queue
  // Binary heap ordered by time, then insertion order.

  function earlier(a, b) {
    return a.t < b.t || (a.t === b.t && a.n < b.n);
  }

  function schedule(sim, t, fn, ...args) {
    const heap = sim.queue;
    heap.push({ t, n: sim.eventCount++, fn, args });
    let i = heap.length - 1;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (!earlier(heap[i], heap[parent])) break;
      [heap[i], heap[parent]] = [heap[parent], heap[i]];
      i = parent;
    }
  }

  function popEvent(heap) {
    const top = heap[0];
    const last = heap.pop();
    if (heap.length) {
      heap[0] = last;
      let i = 0;
      for (;;) {
        const left = 2 * i + 1;
        const right = left + 1;
        let first = i;
        if (left < heap.length && earlier(heap[left], heap[first])) first = left;
        if (right < heap.length && earlier(heap[right], heap[first])) first = right;
        if (first === i) break;
        [heap[i], heap[first]] = [heap[first], heap[i]];
        i = first;
      }
    }
    return top;
  }

  // Process every event up to `until` seconds of simulated time.
  function run(sim, until) {
    while (sim.queue.length && sim.queue[0].t <= until) {
      const ev = popEvent(sim.queue);
      sim.now = ev.t;
      ev.fn(sim, ...ev.args);
    }
    sim.now = Math.max(sim.now, until);
  }

  // ---------------------------------------------------------------- simulation

  function createSim(options) {
    const opts = options || {};
    const cfg = Object.freeze(Object.assign({}, DEFAULTS, opts.config));
    const seed = opts.seed === undefined ? 1 : opts.seed;
    const sim = {
      cfg,
      seed,
      random: typeof opts.random === "function" ? opts.random : mulberry32(seed),
      now: 0,
      queue: [],
      eventCount: 0,
      nodes: LAYOUT.map(([id, x, y]) => ({
        id, x, y,
        isBase: id === BASE_ID,
        alive: true,
        neighbours: [],
        seq: 0,
        seen: new Set(),
        seenOrder: [],
        outbox: [],
        txScheduled: false,
        txUntil: 0,
      })),
      sos: new Map(),        // every SOS created, keyed "origin:seq"
      incidents: new Map(),  // SOS the base has received, same keys, in arrival order
      transmissions: [],     // { sender, receivers, start, end, type, priority }; the UI drains this list
      messages: [],          // { time, text } status messages; the UI drains this list
      misses: [],            // { x, y, time } clicks with no working node in reach; the UI drains this list
      stats: {
        sosSent: 0, reachedBase: 0, transmissions: 0, duplicatesDropped: 0, gaveUp: 0,
        retries: 0, forwards: 0, hopLimitDrops: 0,
      },
    };
    for (const a of sim.nodes) {
      for (const b of sim.nodes) {
        if (a !== b && Math.hypot(a.x - b.x, a.y - b.y) <= cfg.radioRange) a.neighbours.push(b.id);
      }
    }
    return sim;
  }

  function nextSeq(node) {
    node.seq = (node.seq + 1) & 0xffff;
    return node.seq;
  }

  function remember(sim, node, key) {
    if (node.seen.has(key)) return;
    node.seen.add(key);
    node.seenOrder.push(key);
    if (node.seenOrder.length > sim.cfg.seenCacheSize) node.seen.delete(node.seenOrder.shift());
  }

  function enqueue(sim, node, pkt) {
    // Highest priority first; equal priority in arrival order.
    let i = node.outbox.length;
    while (i > 0 && node.outbox[i - 1].priority < pkt.priority) i--;
    node.outbox.splice(i, 0, pkt);
    scheduleTx(sim, node);
  }

  function scheduleTx(sim, node) {
    if (node.txScheduled || !node.outbox.length) return;
    node.txScheduled = true;
    schedule(sim, sim.now + uniform(sim, 0, sim.cfg.jitterMax), tryTx, node);
  }

  function tryTx(sim, node) {
    node.txScheduled = false;
    if (!node.alive || !node.outbox.length) return;
    if (node.txUntil > sim.now) {  // still transmitting: try again shortly
      node.txScheduled = true;
      schedule(sim, sim.now + uniform(sim, sim.cfg.busyBackoff[0], sim.cfg.busyBackoff[1]), tryTx, node);
      return;
    }
    transmit(sim, node, node.outbox.shift());
  }

  function transmit(sim, node, pkt) {
    const duration = airtime(packetSize(pkt), sim.cfg);
    node.txUntil = sim.now + duration;
    sim.stats.transmissions++;
    const receivers = node.neighbours.filter((id) => sim.nodes[id].alive);
    sim.transmissions.push({
      sender: node.id, receivers, start: sim.now, end: sim.now + duration,
      type: pkt.type, priority: pkt.priority,
    });
    schedule(sim, sim.now + duration, finishTx, node, pkt, receivers);
  }

  function finishTx(sim, node, pkt, receivers) {
    if (node.alive) scheduleTx(sim, node);
    // Neighbours alive at the start that are still alive receive the packet,
    // even if the sender was destroyed meanwhile (as in the Python simulator).
    for (const id of receivers) receive(sim, sim.nodes[id], pkt);
  }

  function receive(sim, node, pkt) {
    if (!node.alive) return;
    const key = msgKey(pkt);
    if (node.seen.has(key)) {
      sim.stats.duplicatesDropped++;
      return;
    }
    remember(sim, node, key);
    const hops = pkt.hopCount + 1;

    if (node.isBase) {
      baseReceive(sim, node, pkt, hops);
      return;
    }

    if (pkt.type === ACK && pkt.refOrigin === node.id) {
      const sos = sim.sos.get(incidentKey(pkt.refOrigin, pkt.refSeq));
      if (sos && sos.confirmedAt === null) {
        sos.confirmedAt = sim.now;
        sos.status = "confirmed";
        say(sim, `${PRIORITY_LABELS[sos.priority]} SOS near node ${sos.origin}: sender sees "Delivered to rescue base".`);
      }
      return;
    }

    if (pkt.hopLimit > 1) {
      sim.stats.forwards++;
      enqueue(sim, node, { ...pkt, hopLimit: pkt.hopLimit - 1, hopCount: hops, path: pkt.path.concat(node.id) });
    } else {
      sim.stats.hopLimitDrops++;
    }
  }

  function baseReceive(sim, base, pkt, hops) {
    if (pkt.type !== SOS) return;  // the base ignores ACK echoes
    const key = incidentKey(pkt.origin, pkt.seq);
    if (!sim.incidents.has(key)) {
      sim.incidents.set(key, {
        key, origin: pkt.origin, seq: pkt.seq, priority: pkt.priority,
        emergencyType: pkt.emergencyType, people: pkt.people, landmark: pkt.landmark,
        createdAt: pkt.createdAt, receivedAt: sim.now, hops, attempt: pkt.attempt,
        path: pkt.path.concat(base.id),
      });
      sim.stats.reachedBase++;
      say(sim, `Base received ${PRIORITY_IN_SENTENCE[pkt.priority]} SOS from node ${pkt.origin} after ${hopsText(hops)}.`);
    }
    if (pkt.origin === base.id) return;
    // Confirm every new attempt, in case an earlier confirmation was lost.
    const ack = {
      type: ACK, origin: base.id, seq: nextSeq(base), attempt: 1, hopLimit: sim.cfg.hopLimit,
      hopCount: 0, priority: pkt.priority, refOrigin: pkt.origin, refSeq: pkt.seq,
      path: [base.id], createdAt: sim.now,
    };
    remember(sim, base, msgKey(ack));
    enqueue(sim, base, ack);
  }

  function checkConfirmation(sim, sos, firstPkt) {
    const node = sim.nodes[sos.origin];
    if (sos.confirmedAt !== null || !node.alive) return;
    if (sos.attempts >= sim.cfg.maxAttempts) {
      sos.status = "failed";
      sim.stats.gaveUp++;
      say(sim, `SOS near node ${sos.origin} gave up after ${sim.cfg.maxAttempts} tries: no working route to the base.`);
      return;
    }
    sos.attempts++;
    sim.stats.retries++;
    say(sim, `No confirmation yet for the SOS near node ${sos.origin}. Sending again (try ${sos.attempts} of ${sim.cfg.maxAttempts}).`);
    const retry = { ...firstPkt, attempt: sos.attempts, hopCount: 0, hopLimit: sim.cfg.hopLimit, path: [node.id] };
    remember(sim, node, msgKey(retry));
    enqueue(sim, node, retry);
    // Doubling wait with a little randomness.
    const wait = sim.cfg.ackTimeout * 2 ** (sos.attempts - 1) + uniform(sim, 0, sim.cfg.ackTimeout * 0.25);
    schedule(sim, sim.now + wait, checkConfirmation, sos, firstPkt);
  }

  // ------------------------------------------------------------------- actions

  function nearestNode(sim, x, y, maxDistance, aliveOnly) {
    const limit = maxDistance === undefined ? Infinity : maxDistance;
    let best = null;
    let bestDistance = Infinity;
    for (const node of sim.nodes) {
      if (aliveOnly && !node.alive) continue;
      const d = Math.hypot(node.x - x, node.y - y);
      if (d < bestDistance) {
        best = node;
        bestDistance = d;
      }
    }
    return best && bestDistance <= limit ? { node: best, distance: bestDistance } : null;
  }

  function normaliseReport(report, cfg) {
    const r = report || {};
    const priority = Number(r.priority);
    const emergencyType = Number(r.emergencyType);
    return {
      priority: PRIORITY_LABELS[priority] ? priority : 3,
      emergencyType: EMERGENCY_TYPES[emergencyType] ? emergencyType : 6,
      people: Math.min(255, Math.max(1, Math.round(Number(r.people)) || 1)),
      landmark: landmark16(String(r.landmark || "").trim(), cfg.maxLandmarkBytes),
    };
  }

  // A person at (x, y) sends an SOS through the nearest working node in Wi-Fi reach.
  // report: { priority, emergencyType, people, landmark }. Returns the SOS record, or null.
  function sendSOS(sim, x, y, report, options) {
    const quiet = Boolean(options && options.quiet);
    const hit = nearestNode(sim, x, y, sim.cfg.wifiReach, true);
    if (!hit) {
      sim.misses.push({ x, y, time: sim.now });
      if (!quiet) {
        say(sim, `No working node within ${sim.cfg.wifiReach} m of that spot. Nodes sit at gathering points, so move closer to one.`);
      }
      return null;
    }
    const node = hit.node;
    const r = normaliseReport(report, sim.cfg);
    const seq = nextSeq(node);
    const sos = {
      key: incidentKey(node.id, seq), origin: node.id, seq,
      priority: r.priority, emergencyType: r.emergencyType, people: r.people, landmark: r.landmark,
      x, y, distance: hit.distance,  // demo only: the base never sees the clicked spot
      createdAt: sim.now, attempts: 1, status: "sending", confirmedAt: null,
    };
    sim.sos.set(sos.key, sos);
    sim.stats.sosSent++;
    if (!quiet) {
      say(sim, `${PRIORITY_LABELS[sos.priority]} SOS handed to node ${node.id} (${Math.round(hit.distance)} m away).`);
    }

    const pkt = {
      type: SOS, origin: node.id, seq, attempt: 1, hopLimit: sim.cfg.hopLimit, hopCount: 0,
      priority: sos.priority, emergencyType: sos.emergencyType, people: sos.people,
      landmark: sos.landmark, path: [node.id], createdAt: sim.now,
    };
    if (node.isBase) {  // an SOS raised at the base station itself
      baseReceive(sim, node, pkt, 0);
      sos.status = "confirmed";
      sos.confirmedAt = sim.now;
      return sos;
    }
    remember(sim, node, msgKey(pkt));
    enqueue(sim, node, pkt);
    schedule(sim, sim.now + sim.cfg.ackTimeout, checkConfirmation, sos, pkt);
    return sos;
  }

  // Destroy or repair the node nearest to (x, y). Returns the node, or null if none is close enough.
  function toggleNode(sim, x, y) {
    const hit = nearestNode(sim, x, y, sim.cfg.toggleReach, false);
    if (!hit) {
      say(sim, "Click closer to a node to destroy or repair it.");
      return null;
    }
    const node = hit.node;
    if (node.isBase) {
      say(sim, "The base station stays up in this demo.");
      return node;
    }
    if (node.alive) {
      node.alive = false;
      node.outbox.length = 0;
      say(sim, `Node ${node.id} destroyed. New SOS messages will route around it.`);
    } else {
      node.alive = true;
      say(sim, `Node ${node.id} repaired.`);
    }
    return node;
  }

  // 20 people near random working nodes send an SOS within the next 10 seconds.
  function crowd(sim) {
    for (let i = 0; i < CROWD.count; i++) {
      schedule(sim, sim.now + uniform(sim, 0, CROWD.windowS), crowdSOS);
    }
    say(sim, `${CROWD.count} people send an SOS over the next ${CROWD.windowS} seconds. Watch critical ones jump the queue.`);
  }

  function crowdSOS(sim) {
    const pool = sim.nodes.filter((n) => n.alive && !n.isBase);
    if (!pool.length) return;
    const node = pick(sim, pool);
    const angle = uniform(sim, 0, 2 * Math.PI);
    const radius = CROWD.radiusM * Math.sqrt(sim.random());
    let roll = sim.random();
    let priority = 0;
    for (const [prio, share] of CROWD_PRIORITY_MIX) {
      if (roll < share) {
        priority = prio;
        break;
      }
      roll -= share;
    }
    sendSOS(sim, node.x + radius * Math.cos(angle), node.y + radius * Math.sin(angle), {
      priority,
      emergencyType: pick(sim, CROWD_TYPES[priority]),
      people: 1 + Math.floor(sim.random() * CROWD.peopleMax),
      landmark: pick(sim, CROWD_LANDMARKS),
    }, { quiet: true });
  }

  // A fresh simulation with the same configuration.
  function reset(sim, seed) {
    const fresh = createSim({ seed: seed === undefined ? sim.seed : seed, config: sim.cfg });
    say(fresh, "Network reset. All nodes working.");
    return fresh;
  }

  const Mesh = {
    DEFAULTS, LAYOUT, BASE_ID, PRIORITY_LABELS, EMERGENCY_TYPES,
    airtime, packetSize, landmark16, toLatLon, mulberry32,
    createSim, run, nearestNode, sendSOS, toggleNode, crowd, reset,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = Mesh;
  else root.Mesh = Mesh;
})(typeof globalThis !== "undefined" ? globalThis : this);
