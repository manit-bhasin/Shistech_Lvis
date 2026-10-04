/* Emergency mesh demo: map drawing, input and dashboard. The simulation itself is in mesh.js. */
(function () {
  "use strict";

  const Mesh = window.Mesh;

  const WORLD_MIN = -350;  // metres shown on both axes, north up
  const WORLD_MAX = 2750;
  const WORLD_SPAN = WORLD_MAX - WORLD_MIN;
  const GRID_STEP_M = 200;
  const SCALE_BAR_M = 500;
  const KEY_STEP_M = 50;
  const KEY_STEP_SHIFT_M = 200;
  const MISS_MARK_MS = 1200;
  const PERSON_R = 5;        // person dot radius, CSS px
  const PERSON_HALO = 6.5;   // map-coloured ring around it
  const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  const PRIORITY_TOKENS = { 3: "--critical", 2: "--urgent", 1: "--supplies", 0: "--safe" };
  const COLOUR_TOKENS = ["--bg", "--map", "--ink", "--muted", "--grid", "--link", "--focus",
    "--critical", "--urgent", "--supplies", "--safe", "--confirmation", "--destroyed"];

  const $ = (id) => document.getElementById(id);
  const canvas = $("map");
  const ctx = canvas.getContext("2d");
  const ui = {
    pointer: $("pointer"), nearest: $("nearest"),
    emergencyType: $("emergency-type"), people: $("people"), landmark: $("landmark"),
    modeSos: $("mode-sos"), modeToggle: $("mode-toggle"),
    crowd: $("crowd"), pause: $("pause"), reset: $("reset"),
    speed: $("speed"), reach: $("reach"), status: $("status"),
    time: $("stat-time"), sent: $("stat-sent"), reached: $("stat-reached"),
    tx: $("stat-tx"), duplicates: $("stat-duplicates"), gaveUp: $("stat-gave-up"),
    dashboard: $("dashboard"), dashboardEmpty: $("dashboard-empty"),
  };
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const darkScheme = window.matchMedia("(prefers-color-scheme: dark)");

  const newSeed = () => Date.now() >>> 0;

  let sim = Mesh.createSim({ seed: newSeed() });
  let mode = "sos";
  let paused = false;
  let speed = Number(ui.speed.value);
  let showReach = ui.reach.value === "on";
  let activeTx = [];       // transmissions still on air, for animation
  let missMarks = [];      // clicks with no node in reach, shown briefly
  let selectedKey = null;  // incident whose route is highlighted
  let userPicked = false;  // once someone picks an incident, new arrivals no longer take the highlight
  let knownIncidents = 0;
  let dashboardState = "";
  const dashboardItems = new Map();
  const cursor = { x: 1200, y: 600, visible: false };  // keyboard crosshair, in metres
  let pointerFocus = false;

  // ------------------------------------------------------------ canvas setup

  const colours = {};
  function readColours() {
    const style = getComputedStyle(document.documentElement);
    for (const token of COLOUR_TOKENS) colours[token] = style.getPropertyValue(token).trim();
  }

  let size = 0;  // canvas width and height in CSS pixels
  let dpr = 1;
  function resize() {
    dpr = window.devicePixelRatio || 1;
    size = canvas.clientWidth;
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
  }

  const sx = (x) => ((x - WORLD_MIN) / WORLD_SPAN) * size;
  const sy = (y) => ((WORLD_MAX - y) / WORLD_SPAN) * size;
  const metres = (m) => (m / WORLD_SPAN) * size;
  const clampWorld = (v) => Math.min(WORLD_MAX, Math.max(WORLD_MIN, v));

  function eventToWorld(e) {
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left - canvas.clientLeft;
    const py = e.clientY - rect.top - canvas.clientTop;
    return { x: WORLD_MIN + (px / size) * WORLD_SPAN, y: WORLD_MAX - (py / size) * WORLD_SPAN };
  }

  // ----------------------------------------------------------------- drawing

  function priorityColour(priority) {
    return colours[PRIORITY_TOKENS[priority]];
  }

  function packetColour(pkt) {
    return pkt.type === "ACK" ? colours["--confirmation"] : priorityColour(pkt.priority);
  }

  function personColour(sos) {
    return sos.status === "failed" ? colours["--destroyed"] : priorityColour(sos.priority);
  }

  // Where to draw a person, in CSS pixels: at the clicked spot, or, when that spot is so close to
  // the node that the dot would cover the node's circle, square or X, pushed out just clear of it.
  function personSpot(sos, nodeR) {
    const node = sim.nodes[sos.origin];
    const nx = sx(node.x);
    const ny = sy(node.y);
    const x = sx(sos.x);
    const y = sy(sos.y);
    const marker = node.isBase ? (nodeR + 2) * Math.SQRT2 : node.alive ? nodeR + 1 : nodeR * Math.SQRT2 + 1.5;
    const clear = marker + PERSON_HALO + 1;
    const d = Math.hypot(x - nx, y - ny);
    if (d >= clear) return { x, y };
    const ux = d > 2 ? (x - nx) / d : 0;  // a click on the node itself goes straight below it
    const uy = d > 2 ? (y - ny) / d : 1;
    return { x: nx + ux * clear, y: ny + uy * clear };
  }

  function circleHitsRect(cx, cy, r, x, y, w, h) {
    const dx = cx - Math.min(Math.max(cx, x), x + w);
    const dy = cy - Math.min(Math.max(cy, y), y + h);
    return dx * dx + dy * dy < r * r;
  }

  function circle(x, y, r) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
  }

  function draw(now) {
    const nodeR = Math.min(8, Math.max(5.5, size / 90));
    const labelPx = Math.max(10, Math.round(size / 58));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = colours["--map"];
    ctx.fillRect(0, 0, size, size);

    // Grid every 200 m.
    ctx.strokeStyle = colours["--grid"];
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let g = Math.ceil(WORLD_MIN / GRID_STEP_M) * GRID_STEP_M; g <= WORLD_MAX; g += GRID_STEP_M) {
      ctx.moveTo(sx(g), 0);
      ctx.lineTo(sx(g), size);
      ctx.moveTo(0, sy(g));
      ctx.lineTo(size, sy(g));
    }
    ctx.stroke();

    // Wi-Fi reach around working nodes.
    if (showReach) {
      ctx.fillStyle = colours["--focus"];
      ctx.globalAlpha = 0.07;
      for (const n of sim.nodes) {
        if (!n.alive) continue;
        circle(sx(n.x), sy(n.y), metres(sim.cfg.wifiReach));
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // Radio links between working neighbours.
    ctx.strokeStyle = colours["--link"];
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const a of sim.nodes) {
      if (!a.alive) continue;
      for (const id of a.neighbours) {
        const b = sim.nodes[id];
        if (a.id < b.id && b.alive) {
          ctx.moveTo(sx(a.x), sy(a.y));
          ctx.lineTo(sx(b.x), sy(b.y));
        }
      }
    }
    ctx.stroke();

    // Route of the selected incident.
    const incident = selectedKey && sim.incidents.get(selectedKey);
    if (incident) {
      ctx.strokeStyle = priorityColour(incident.priority);
      ctx.lineWidth = 4;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      incident.path.forEach((id, i) => {
        const n = sim.nodes[id];
        if (i === 0) ctx.moveTo(sx(n.x), sy(n.y));
        else ctx.lineTo(sx(n.x), sy(n.y));
      });
      ctx.stroke();
      ctx.lineCap = "butt";
    }

    // Dashed line from each person to their node (the dots themselves are drawn after the nodes).
    const people = Array.from(sim.sos.values(), (sos) => ({ sos, ...personSpot(sos, nodeR) }));
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    for (const p of people) {
      const node = sim.nodes[p.sos.origin];
      ctx.strokeStyle = personColour(p.sos);
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(sx(node.x), sy(node.y));
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // Transmissions: a dot travels along each link for the packet's airtime, plus a ring at the sender.
    for (const tx of activeTx) {
      const k = Math.min(1, Math.max(0, (sim.now - tx.start) / (tx.end - tx.start)));
      const sender = sim.nodes[tx.sender];
      const colour = packetColour(tx);
      if (!reducedMotion.matches) {
        ctx.strokeStyle = colour;
        ctx.lineWidth = 2;
        ctx.globalAlpha = 1 - k;
        circle(sx(sender.x), sy(sender.y), nodeR + 2 + 10 * k);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
      ctx.fillStyle = colour;
      for (const id of tx.receivers) {
        const r = sim.nodes[id];
        circle(sx(sender.x + (r.x - sender.x) * k), sy(sender.y + (r.y - sender.y) * k), 3.5);
        ctx.fill();
      }
    }

    // Nodes.
    for (const n of sim.nodes) {
      const x = sx(n.x);
      const y = sy(n.y);
      if (n.isBase) {
        const half = nodeR + 2;
        ctx.fillStyle = colours["--ink"];
        ctx.fillRect(x - half, y - half, 2 * half, 2 * half);
        ctx.fillStyle = colours["--map"];
        ctx.font = `600 ${Math.round(labelPx * 0.9)}px ${FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("B", x, y + 0.5);
      } else if (!n.alive) {
        ctx.strokeStyle = colours["--destroyed"];
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(x - nodeR, y - nodeR);
        ctx.lineTo(x + nodeR, y + nodeR);
        ctx.moveTo(x + nodeR, y - nodeR);
        ctx.lineTo(x - nodeR, y + nodeR);
        ctx.stroke();
      } else {
        ctx.fillStyle = colours["--map"];
        ctx.strokeStyle = colours["--ink"];
        ctx.lineWidth = 2;
        circle(x, y, nodeR);
        ctx.fill();
        ctx.stroke();
        if (n.outbox.length) {  // colour of the next packet waiting to be sent
          ctx.fillStyle = packetColour(n.outbox[0]);
          circle(x, y, nodeR - 3);
          ctx.fill();
        }
      }
    }

    // People: hollow while sending, filled with a check mark when confirmed, grey if it failed.
    // Drawn above the routes and nodes, but never on a node's own marker (see personSpot).
    for (const { sos, x, y } of people) {
      const colour = personColour(sos);
      ctx.fillStyle = colours["--map"];  // halo that separates the dot from a route below
      circle(x, y, PERSON_HALO);
      ctx.fill();
      circle(x, y, PERSON_R);
      if (sos.status === "confirmed") {
        ctx.fillStyle = colour;
        ctx.fill();
        ctx.strokeStyle = colours["--map"];
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(x - 2.5, y);
        ctx.lineTo(x - 0.5, y + 2);
        ctx.lineTo(x + 2.8, y - 2.2);
        ctx.stroke();
      } else {
        ctx.strokeStyle = colour;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    // Node IDs last, outlined in the map colour so a route never hides them. Each ID sits up and to
    // the right of its node, or in the first other corner that none of its people's dots cover.
    ctx.font = `${labelPx}px ${FONT}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;
    ctx.strokeStyle = colours["--map"];
    const ascent = labelPx * 0.75;
    for (const n of sim.nodes) {
      const text = String(n.id);
      const w = ctx.measureText(text).width;
      const x = sx(n.x);
      const y = sy(n.y);
      const corners = [
        [x + nodeR + 3, y - nodeR - 1], [x - nodeR - 3 - w, y - nodeR - 1],
        [x + nodeR + 3, y + nodeR + 1 + ascent], [x - nodeR - 3 - w, y + nodeR + 1 + ascent],
      ];
      const mine = people.filter((p) => p.sos.origin === n.id);
      const [lx, ly] = corners.find(([cx, cy]) => !mine.some((p) =>
        circleHitsRect(p.x, p.y, PERSON_HALO, cx - 1.5, cy - ascent - 1.5, w + 3, ascent + 3))) || corners[0];
      ctx.strokeText(text, lx, ly);
      ctx.fillStyle = n.alive ? colours["--ink"] : colours["--destroyed"];
      ctx.fillText(text, lx, ly);
    }

    // Clicks with no working node in Wi-Fi reach.
    ctx.strokeStyle = colours["--muted"];
    ctx.lineWidth = 2;
    for (const mark of missMarks) {
      const k = (now - mark.born) / MISS_MARK_MS;
      ctx.globalAlpha = Math.max(0, 1 - k);
      circle(sx(mark.x), sy(mark.y), reducedMotion.matches ? 8 : 6 + 14 * k);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // 500 m scale bar along the bottom edge, below the lowest nodes, labelled on its right.
    const barX = 12;
    const barY = size - 9;
    const barW = metres(SCALE_BAR_M);
    ctx.strokeStyle = colours["--muted"];
    ctx.fillStyle = colours["--muted"];
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(barX, barY - 4);
    ctx.lineTo(barX, barY);
    ctx.lineTo(barX + barW, barY);
    ctx.lineTo(barX + barW, barY - 4);
    ctx.stroke();
    ctx.font = `${labelPx}px ${FONT}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(`${SCALE_BAR_M} m`, barX + barW + 6, barY - 2);

    // Keyboard crosshair.
    if (cursor.visible) {
      const x = sx(cursor.x);
      const y = sy(cursor.y);
      ctx.strokeStyle = colours["--focus"];
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x - 11, y);
      ctx.lineTo(x + 11, y);
      ctx.moveTo(x, y - 11);
      ctx.lineTo(x, y + 11);
      ctx.stroke();
    }
  }

  // --------------------------------------------------------------- dashboard

  function dashboardItem(incident) {
    let item = dashboardItems.get(incident.key);
    if (item) return item;
    const sos = sim.sos.get(incident.key);
    const node = sim.nodes[incident.origin];
    const { lat, lon } = Mesh.toLatLon(node.x, node.y);
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "incident";
    button.dataset.priority = String(incident.priority);
    const [title, where, meta, demo] = ["title", "where", "meta", "demo"].map((name) => {
      const span = document.createElement("span");
      span.className = `incident-${name}`;
      button.append(span);
      return span;
    });
    const people = `${incident.people} ${incident.people === 1 ? "person" : "people"}`;
    title.textContent = `${Mesh.PRIORITY_LABELS[incident.priority]}: ${Mesh.EMERGENCY_TYPES[incident.emergencyType]}, ${people}`;
    where.textContent = `Node ${incident.origin} at ${lat.toFixed(5)}, ${lon.toFixed(5)}` +
      (incident.landmark ? `, "${incident.landmark}"` : "");
    demo.textContent = `Demo only: clicked spot was ${Math.round(sos.distance)} m from the node.`;
    button.addEventListener("click", () => {
      selectedKey = incident.key;
      userPicked = true;
      syncDashboard();
    });
    li.append(button);
    item = { li, button, meta };
    dashboardItems.set(incident.key, item);
    return item;
  }

  function syncDashboard() {
    if (sim.incidents.size > knownIncidents) {
      knownIncidents = sim.incidents.size;
      if (!userPicked) selectedKey = Array.from(sim.incidents.keys()).pop();  // default: newest arrival
    }
    const incidents = Array.from(sim.incidents.values())
      .sort((a, b) => b.priority - a.priority || a.receivedAt - b.receivedAt);
    const status = (incident) => sim.sos.get(incident.key).status;
    const state = incidents.map((i) => i.key + status(i)[0]).join(",") + "|" + selectedKey;
    if (state === dashboardState) return;
    dashboardState = state;

    ui.dashboardEmpty.hidden = incidents.length > 0;
    for (const incident of incidents) {
      const item = dashboardItem(incident);
      const hops = `${incident.hops} hop${incident.hops === 1 ? "" : "s"}`;
      const delay = (incident.receivedAt - incident.createdAt).toFixed(1);
      const sender = { confirmed: "Sender has confirmation.", sending: "Confirmation on its way.",
        failed: "Sender gave up waiting for confirmation." }[status(incident)];
      item.meta.textContent = `${hops}, ${delay} s, send ${incident.attempt}. ${sender}`;
      item.button.setAttribute("aria-pressed", String(incident.key === selectedKey));
    }

    const items = incidents.map((incident) => dashboardItems.get(incident.key).li);
    const current = ui.dashboard.children;
    const sameOrder = items.length === current.length && items.every((li, i) => current[i] === li);
    if (!sameOrder) {
      const active = document.activeElement;
      const focused = active !== ui.dashboard && ui.dashboard.contains(active) ? active : null;
      const focusedTop = focused ? focused.getBoundingClientRect().top : 0;
      const scrollTop = ui.dashboard.scrollTop;
      ui.dashboard.replaceChildren(...items);
      if (focused) {
        // Moving a focused button drops its focus and resets the list's scroll. Give focus back and
        // keep that incident where it was on screen, even when a new one was inserted above it.
        if (document.activeElement !== focused) focused.focus({ preventScroll: true });
        ui.dashboard.scrollTop += focused.getBoundingClientRect().top - focusedTop;
        // While the list is too short to scroll, move the page instead.
        const rest = focused.getBoundingClientRect().top - focusedTop;
        if (Math.abs(rest) > 0.5) window.scrollBy(0, rest);
      } else {
        ui.dashboard.scrollTop = scrollTop;
      }
    }
  }

  // ------------------------------------------------------- status and stats

  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  function syncMessages() {
    const messages = sim.messages.splice(0);
    if (messages.length) ui.status.textContent = messages[messages.length - 1].text;
  }

  function syncStats() {
    setText(ui.time, `${sim.now.toFixed(1)} s`);
    setText(ui.sent, String(sim.stats.sosSent));
    setText(ui.reached, String(sim.incidents.size));
    setText(ui.tx, String(sim.stats.transmissions));
    setText(ui.duplicates, String(sim.stats.duplicatesDropped));
    setText(ui.gaveUp, String(sim.stats.gaveUp));
  }

  // ------------------------------------------------------------------- input

  function readReport() {
    return {
      priority: Number(document.querySelector('input[name="priority"]:checked').value),
      emergencyType: Number(ui.emergencyType.value),
      people: Number(ui.people.value),
      landmark: ui.landmark.value,
    };
  }

  function actAt(x, y) {
    if (mode === "sos") Mesh.sendSOS(sim, x, y, readReport());
    else Mesh.toggleNode(sim, x, y);
    syncMessages();
    updateReadout(x, y);  // a destroyed or repaired node changes the nearest one; a tap has no pointer move
  }

  let readoutAt = null;  // last spot shown in the readout, refreshed after Reset
  function updateReadout(x, y) {
    readoutAt = { x, y };
    const { lat, lon } = Mesh.toLatLon(x, y);
    setText(ui.pointer, `${lat.toFixed(5)}, ${lon.toFixed(5)}`);
    const hit = Mesh.nearestNode(sim, x, y, Infinity, true);
    const outOfReach = hit && hit.distance > sim.cfg.wifiReach ? " (out of Wi-Fi reach)" : "";
    setText(ui.nearest, hit ? `node ${hit.node.id}, ${Math.round(hit.distance)} m${outOfReach}` : "–");
  }

  canvas.addEventListener("pointerdown", () => {
    pointerFocus = true;
  });
  canvas.addEventListener("focus", () => {
    // Show the crosshair when the map is reached with the keyboard, not on a mouse click.
    cursor.visible = !pointerFocus;
    pointerFocus = false;
    if (cursor.visible) updateReadout(cursor.x, cursor.y);
  });
  canvas.addEventListener("blur", () => {
    cursor.visible = false;
    pointerFocus = false;
  });
  canvas.addEventListener("click", (e) => {
    const p = eventToWorld(e);
    cursor.x = clampWorld(p.x);
    cursor.y = clampWorld(p.y);
    actAt(p.x, p.y);
  });
  canvas.addEventListener("pointermove", (e) => {
    const p = eventToWorld(e);
    updateReadout(p.x, p.y);
  });
  canvas.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const step = e.shiftKey ? KEY_STEP_SHIFT_M : KEY_STEP_M;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (moves[e.key]) {
      e.preventDefault();
      cursor.x = clampWorld(cursor.x + moves[e.key][0]);
      cursor.y = clampWorld(cursor.y + moves[e.key][1]);
      cursor.visible = true;
      updateReadout(cursor.x, cursor.y);
    } else if (e.key === "Enter") {
      e.preventDefault();
      cursor.visible = true;
      if (!e.repeat) actAt(cursor.x, cursor.y);
    } else if (e.key === " ") {
      e.preventDefault();  // stop the page scrolling; Space acts on keyup
    }
  });
  canvas.addEventListener("keyup", (e) => {
    if (e.key !== " " || e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    cursor.visible = true;
    actAt(cursor.x, cursor.y);
  });

  function setMode(next) {
    mode = next;
    ui.modeSos.setAttribute("aria-pressed", String(next === "sos"));
    ui.modeToggle.setAttribute("aria-pressed", String(next === "toggle"));
  }
  ui.modeSos.addEventListener("click", () => setMode("sos"));
  ui.modeToggle.addEventListener("click", () => setMode("toggle"));

  ui.crowd.addEventListener("click", () => {
    Mesh.crowd(sim);
    syncMessages();
  });
  ui.pause.addEventListener("click", () => {
    paused = !paused;
    ui.pause.textContent = paused ? "Resume" : "Pause";
  });
  ui.reset.addEventListener("click", () => {
    sim = Mesh.reset(sim, newSeed());
    activeTx = [];
    missMarks = [];
    selectedKey = null;
    userPicked = false;
    knownIncidents = 0;
    dashboardState = "";
    dashboardItems.clear();
    ui.dashboard.replaceChildren();
    syncMessages();
    if (readoutAt) updateReadout(readoutAt.x, readoutAt.y);
  });
  ui.speed.addEventListener("change", () => {
    speed = Number(ui.speed.value);
  });
  ui.reach.addEventListener("change", () => {
    showReach = ui.reach.value === "on";
  });

  // --------------------------------------------------------------- main loop

  let lastTime = performance.now();
  function frame(time) {
    const dt = Math.min(0.05, Math.max(0, (time - lastTime) / 1000));  // no big jumps after a hidden tab
    lastTime = time;
    if (!paused) Mesh.run(sim, sim.now + dt * speed);

    activeTx = activeTx.concat(sim.transmissions.splice(0)).filter((tx) => tx.end > sim.now);
    for (const miss of sim.misses.splice(0)) missMarks.push({ x: miss.x, y: miss.y, born: time });
    missMarks = missMarks.filter((mark) => time - mark.born < MISS_MARK_MS);

    syncMessages();
    syncDashboard();
    syncStats();
    if ((window.devicePixelRatio || 1) !== dpr) resize();
    if (size > 0) draw(time);
    requestAnimationFrame(frame);
  }

  readColours();
  darkScheme.addEventListener("change", readColours);
  resize();
  // Resizing clears the canvas, and this callback runs after the frame was drawn, so redraw here.
  new ResizeObserver(() => {
    resize();
    if (size > 0) draw(performance.now());
  }).observe(canvas);
  syncDashboard();
  requestAnimationFrame(frame);
})();
