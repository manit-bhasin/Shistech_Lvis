"""Build docs/technical-reference.pdf from the simulator code, its results and the README.

Every number in the PDF is read from the repository or computed from it here. Where the
README and the code state the same value, the script checks that they agree and stops if
they don't.

Run from anywhere (Python 3.12 tested):
    pip install -r docs/tools/requirements.txt
    python docs/tools/build_technical_reference.py

The PDF is printed by your installed Google Chrome through Playwright, so no browser
download is needed. Options: --out PATH, --html PATH (also save the HTML), --no-check.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import html
import inspect
import math
import re
import struct
import subprocess
import textwrap
import sys
from collections import deque
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "emergency-mesh" / "simulation"
RESULTS = SIM / "results"
README = ROOT / "README.md"
WOKWI = ROOT / "emergency-mesh" / "wokwi_node" / "main.py"
FACTS = Path(__file__).with_name("outside_facts.csv")
DEFAULT_OUT = ROOT / "docs" / "technical-reference.pdf"
sys.path.insert(0, str(SIM))

from meshsim import network, protocol, scenarios  # noqa: E402
from meshsim.network import RadioConfig  # noqa: E402
from meshsim.node import NodeConfig  # noqa: E402
from meshsim.protocol import (EmergencyType, Flag, PacketType, Priority, decode,  # noqa: E402
                              encode, lora_airtime)


class BuildError(SystemExit):
    pass


def need(cond, msg):
    if not cond:
        raise BuildError(f"build_technical_reference: {msg}")


# ----------------------------------------------------------------- text helpers
esc = html.escape


def md_inline(text: str) -> str:
    """The small part of Markdown the README uses inside tables and lists."""
    out = esc(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)

    def link(m):
        label, url = m.group(1), m.group(2)
        return f'<a href="{url}">{label}</a>' if url.startswith("http") else label
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, out)


def num(x: float, places: int = 1) -> str:
    return f"{x:.{places}f}"


def ms(seconds: float, places: int = 1) -> str:
    return f"{seconds * 1000:.{places}f} ms"


def pct(x: float, places: int = 2) -> str:
    return f"{x:.{places}f}%"


def py_value(v) -> str:
    """A config value the way it reads in Python, without a trailing .0."""
    if isinstance(v, tuple):
        return "(" + ", ".join(f"{x:g}" for x in v) + ")"
    return f"{v:g}"


def dur(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds / 3600:g} h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


# --------------------------------------------------------------- README reading
README_TEXT = README.read_text(encoding="utf-8").replace("\r\n", "\n")


def section(prefix: str) -> list:
    """Lines of the README section whose heading starts with `prefix`, up to the next ## heading."""
    lines = README_TEXT.split("\n")
    starts = [i for i, l in enumerate(lines) if l.startswith(prefix)]
    need(len(starts) == 1, f"README heading {prefix!r} not found exactly once")
    body = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def tables(lines: list) -> list:
    """[(caption, header, rows)] for each Markdown table, captioned by the bold line above it."""
    found, caption, i = [], "", 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            rows = [r for r in block if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
            found.append((caption, rows[0], rows[1:]))
            continue
        if line:
            caption = line
        i += 1
    return found


def table_with(lines: list, caption_part: str):
    hits = [t for t in tables(lines) if caption_part in t[0]]
    need(len(hits) == 1, f"README table captioned {caption_part!r} not found exactly once")
    return hits[0]


def bullets(lines: list) -> dict:
    """{bold subheading: [bullet text]} for '- ' bullets, grouped under lines like **System:**."""
    groups, current = {}, ""
    for line in lines:
        m = re.fullmatch(r"\*\*(.+?):\*\*", line.strip())
        if m:
            current = m.group(1)
            continue
        if line.startswith("- "):
            groups.setdefault(current, []).append(line[2:].strip())
    return groups


def numbered_after(lines: list, marker: str) -> list:
    start = [i for i, l in enumerate(lines) if l.startswith(marker)]
    need(len(start) == 1, f"README line starting {marker!r} not found")
    items = []
    for line in lines[start[0] + 1:]:
        m = re.match(r"\d+\.\s+(.*)", line)
        if not m:
            break
        items.append(m.group(1))
    return items


def numbers_in(text: str) -> set:
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)}


def regex(pattern: str, text: str, what: str, flags=0):
    m = re.search(pattern, text, flags)
    need(m is not None, f"could not find {what}")
    return m


# ----------------------------------------------------------------------- git
def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def git_state(out: Path) -> tuple:
    try:
        commit = git("rev-parse", "--short", "HEAD")
        changed = git("status", "--porcelain").splitlines()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False
    try:
        skip = out.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        skip = None
    dirty = [c for c in changed if not (skip and c[3:].strip('"') == skip)]
    return commit, bool(dirty)


# ------------------------------------------------------------ packet layouts
PROTO_SRC = inspect.getsource(protocol)
LAYOUT_RE = re.compile(r"#\s*([a-z_, ]+?)\s*->\s*(\d+) bytes[^\n]*\n(\w+) = struct\.Struct\(\"([^\"]+)\"\)")
CTYPE = {"B": "uint8", "H": "uint16", "I": "uint32"}


def layouts() -> dict:
    """{struct name: [(field, offset in struct, size, format char)]} from protocol.py."""
    found = {}
    for names, nbytes, var, fmt in LAYOUT_RE.findall(PROTO_SRC):
        names = [n.strip() for n in names.split(",")]
        st = getattr(protocol, var)
        need(st.format == fmt and fmt[0] == "<", f"{var} format changed")
        need(len(fmt) - 1 == len(names), f"{var}: comment names {len(names)} fields, format has {len(fmt) - 1}")
        need(st.size == int(nbytes), f"{var}: comment says {nbytes} bytes, struct is {st.size}")
        fields, off = [], 0
        for name, code in zip(names, fmt[1:]):
            size = struct.calcsize("<" + code)
            fields.append((name, off, size, code))
            off += size
        found[var] = fields
    need({"HEADER", "SOS_BODY", "ACK_BODY", "HEARTBEAT_BODY"} <= found.keys(), "a packet layout is missing")
    return found


def packet_fields(lay: dict, body: str, with_landmark: bool = False) -> list:
    """[(field, offset, size, format char, group)] for a whole packet."""
    out = [(n, o, s, c, "header") for n, o, s, c in lay["HEADER"]]
    base = protocol.HEADER.size
    group = {"SOS_BODY": "sos", "ACK_BODY": "ack", "HEARTBEAT_BODY": "hb"}[body]
    out += [(n, base + o, s, c, group) for n, o, s, c in lay[body]]
    if with_landmark:
        out.append(("landmark", base + getattr(protocol, body).size, protocol.MAX_LANDMARK_BYTES, "s", "landmark"))
    return out


FILL = {"header": "#ffd43b", "sos": "#c9d8ff", "ack": "#bfe5cc", "hb": "#ffd9b3", "landmark": "#ebe5d6"}
GROUP_NAME = {"header": "Header", "sos": "SOS body", "ack": "ACK body", "hb": "Heartbeat body",
              "landmark": "Landmark (optional)"}


def bytemap_svg(fields: list, total: int) -> str:
    """Byte map: one cell per byte, offsets above, field names hanging below at 45 degrees."""
    cell = min(34.0, 664 / total)
    x0, top, h = 4, 16, 26
    width = 672
    longest = max(len(f[0]) for f in fields)
    height = top + h + 14 + longest * 5.6 * 0.72 + 12
    parts = [f'<svg class="bytemap" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" '
             f'height="{height:.0f}" xmlns="http://www.w3.org/2000/svg" role="img">']
    for b in range(total):
        x = x0 + b * cell
        parts.append(f'<text x="{x + cell / 2:.1f}" y="{top - 5}" class="bm-off">{b}</text>')
    for name, off, size, _code, group in fields:
        x = x0 + off * cell
        parts.append(f'<rect x="{x:.1f}" y="{top}" width="{cell * size:.1f}" height="{h}" '
                     f'fill="{FILL[group]}" stroke="#141414" stroke-width="1.2"/>')
        for k in range(1, size):
            xs = x + k * cell
            parts.append(f'<line x1="{xs:.1f}" y1="{top + h - 6}" x2="{xs:.1f}" y2="{top + h}" '
                         f'stroke="#141414" stroke-width="0.6"/>')
        cx, y = x + cell * size / 2, top + h + 12
        parts.append(f'<line x1="{x + 2:.1f}" y1="{top + h + 5}" x2="{x + cell * size - 2:.1f}" '
                     f'y2="{top + h + 5}" stroke="#141414" stroke-width="1"/>')
        parts.append(f'<text x="{cx:.1f}" y="{y}" class="bm-name" '
                     f'transform="rotate(45 {cx:.1f} {y})">{esc(name)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def legend(groups: list) -> str:
    return '<p class="legend">' + "".join(
        f'<span><i style="background:{FILL[g]}"></i>{GROUP_NAME[g]}</span>' for g in groups) + "</p>"


# ------------------------------------------------------------------- airtime
AIRTIME_SRC = inspect.getsource(lora_airtime)
AIR_RE = (r"low_dr = 1 if t_sym > ([\d.]+).*?"
          r"numerator = (\d+) \* payload_bytes - (\d+) \* spreading_factor \+ (\d+) \+ (\d+) \* int\(crc\) - (\d+) \* ih.*?"
          r"payload_symbols = (\d+) \+ max\(\s*math\.ceil\(numerator / \((\d+) \* \(spreading_factor - (\d+) \* low_dr\)\)\)"
          r" \* \(coding_rate \+ (\d+)\), 0\).*?"
          r"return \(preamble_symbols \+ ([\d.]+)\)")
_m = regex(AIR_RE, AIRTIME_SRC, "the time-on-air formula in lora_airtime()", re.S)
K = dict(zip(["ldr_s", "n8", "n4", "n28", "ncrc", "nih", "p8", "d4", "d2", "cr4", "pre"],
             [float(v) for v in _m.groups()]))
AIR_DEFAULTS = {k: v.default for k, v in inspect.signature(lora_airtime).parameters.items()
                if v.default is not inspect.Parameter.empty}


def air_steps(size: int, sf: int, bw: int) -> dict:
    """The time-on-air formula one step at a time, with the constants read from protocol.py."""
    d = AIR_DEFAULTS
    t_sym = 2 ** sf / bw
    ldr = 1 if t_sym > K["ldr_s"] else 0
    ih = 0 if d["explicit_header"] else 1
    numer = K["n8"] * size - K["n4"] * sf + K["n28"] + K["ncrc"] * int(d["crc"]) - K["nih"] * ih
    denom = K["d4"] * (sf - K["d2"] * ldr)
    blocks = math.ceil(numer / denom)
    pay = K["p8"] + max(blocks * (d["coding_rate"] + K["cr4"]), 0)
    pre = d["preamble_symbols"] + K["pre"]
    total = (pre + pay) * t_sym
    need(abs(total - lora_airtime(size, sf, bw)) < 1e-12, f"step-by-step airtime differs for {size} B at SF{sf}")
    return dict(t_sym=t_sym, ldr=ldr, ih=ih, numer=numer, denom=denom, blocks=blocks,
                pay=pay, pre=pre, total=total)


# ------------------------------------------------------------------ CSV files
def read_csv(name: str) -> tuple:
    with open(RESULTS / name, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


LABELS = {
    "priority": "Priority", "type": "Type", "node": "Node", "lat": "Latitude", "lon": "Longitude",
    "people": "People", "flags": "Flags", "landmark": "Landmark", "hops": "Hops", "delay_s": "Delay (s)",
    "failed_pct": "Relays destroyed (%)", "sos_sent": "SOS sent", "reachable_pct": "Reachable (%)",
    "delivered_pct": "Delivered (%)", "delivered_of_reachable_pct": "Delivered of reachable (%)",
    "confirmed_pct": "Sender confirmed (%)", "median_delay_s": "Median delay (s)",
    "mean_hops": "Mean hops", "tx_per_sos": "Transmissions per SOS",
    "median_delay_critical_s": "Median delay, critical (s)", "median_delay_urgent_s": "Median delay, urgent (s)",
    "median_delay_supplies_s": "Median delay, supplies (s)", "median_delay_safe_s": "Median delay, \"I'm safe\" (s)",
    "collisions_per_sos": "Collisions per SOS", "trials": "Runs", "nodes_killed": "Nodes killed",
    "detected": "Detected", "missed": "Missed", "false_alarms": "False alarms",
    "mean_minutes_to_alert": "Mean time to alert (min)", "max_minutes_to_alert": "Longest time to alert (min)",
    "range_m": "Range (m)", "loss_pct": "Random loss (%)",
}


def label(col: str) -> str:
    if col in LABELS:
        return LABELS[col]
    m = re.fullmatch(r"p(\d+)_delay_s", col)
    if m:
        return f"{m.group(1)}th percentile delay (s)"
    m = re.fullmatch(r"sos_in_(\d+)s", col)
    if m:
        return f"SOS in {m.group(1)} s"
    m = re.fullmatch(r"within_(\d+)min_pct", col)
    if m:
        return f"Delivered within {m.group(1)} min (%)"
    return col


def csv_table(name: str, cls: str = "") -> str:
    header, rows = read_csv(name)
    head = "".join(f"<th>{esc(label(c))}<span>{esc(c)}</span></th>" for c in header)
    body = "".join("<tr>" + "".join(f"<td>{'n/a' if v == 'nan' else esc(v)}</td>" for v in r) + "</tr>"
                   for r in rows)
    cls += " keep" if len(rows) <= 10 else ""
    return (f'<table class="data {cls}" data-name="{name}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>"
            f'<p class="src">From <code>emergency-mesh/simulation/results/{name}</code>; '
            f"{len(rows)} row{'s' if len(rows) != 1 else ''}, every column.</p>")


def image(name: str, alt: str) -> str:
    data = base64.b64encode((RESULTS / name).read_bytes()).decode()
    return (f'<figure><img src="data:image/png;base64,{data}" alt="{esc(alt)}">'
            f"<figcaption><code>results/{name}</code>: {esc(alt)}</figcaption></figure>")


def simple_table(header: list, rows: list, cls: str = "", name: str = "") -> str:
    head = "".join(f"<th>{h}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    cls += " keep" if len(rows) <= 10 else ""
    return f'<table class="{cls}" data-name="{name}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


# ---------------------------------------------------------------------- build
def build_html(today: dt.date, commit: str, dirty: bool) -> str:
    lay = layouts()
    radio, cfg = RadioConfig(), NodeConfig()
    sf, bw = radio.spreading_factor, radio.bandwidth_hz
    cr = AIR_DEFAULTS["coding_rate"]
    hdr = protocol.HEADER.size
    size_sos_min = hdr + protocol.SOS_BODY.size
    size_sos_max = protocol.MAX_SOS_BYTES
    size_ack = hdr + protocol.ACK_BODY.size
    size_hb = hdr + protocol.HEARTBEAT_BODY.size
    run_src = (SIM / "run.py").read_text(encoding="utf-8")

    # run.py settings that produced results/
    trials = int(regex(r'"--trials", type=int, default=(\d+)', run_src, "--trials default").group(1))
    seed = int(regex(r'"--seed", type=int, default=(\d+)', run_src, "--seed default").group(1))
    load_div = int(regex(r"def run_load.*?trials = max\(1, args\.trials // (\d+)\)", run_src, "load trials", re.S).group(1))
    health_div = int(regex(r"scenarios\.health\(trials=max\(1, args\.trials // (\d+)\)", run_src, "health trials").group(1))
    sf_lo, sf_hi = map(int, regex(r"LoRa spreading factor \((\d+)-(\d+)\)", run_src, "--sf range").groups())
    seeds_m = regex(r"We repeated the tests with seeds (\d+) and (\d+)", README_TEXT, "repeat seeds")

    # the rerouting demo network, exactly as `python run.py demo` builds it
    demo = scenarios.demo(seed=seed)
    sim = demo.sim
    nodes = list(sim.nodes.values())
    relays = [n for n in nodes if not n.is_base]
    base = sim.nodes[0]
    first = min(sim.nodes[demo.origin].sent_sos.values(), key=lambda o: o.packet.seq).packet
    dash_header, dash_rows = read_csv("demo_dashboard.csv")
    first_row = dict(zip(dash_header, dash_rows[0]))
    need(first_row["priority"] == Priority(first.priority).name and int(first_row["node"]) == first.origin
         and first_row["landmark"] == first.landmark, "the demo SOS does not match demo_dashboard.csv row 1")
    need(decode(encode(first)) == first, "encode/decode round trip failed for the demo SOS")
    raw = encode(first)

    # hops from the base in the demo network (breadth-first, ignoring destroyed nodes)
    hops = {0: 0}
    queue = deque([0])
    while queue:
        cur = queue.popleft()
        for nxt in sim.neighbours(cur):
            if nxt not in hops:
                hops[nxt] = hops[cur] + 1
                queue.append(nxt)
    need(len(hops) == len(nodes), "the demo network is not fully connected")
    degree = [len(sim.neighbours(n.id)) for n in relays]
    max_hops = max(hops.values())
    need(max_hops < cfg.hop_limit, "a node is beyond the hop limit")

    gsig = inspect.signature(scenarios.grid_nodes).parameters
    g_rows, g_cols = gsig["rows"].default, gsig["cols"].default
    spacing, jitter = gsig["spacing_m"].default, gsig["jitter_m"].default

    # ------------------------------------------------------------------ cover
    team = regex(r"\*\*(\w+), (SHISTECH \d{4}):\*\* (.+)", README_TEXT, "the team line")
    team_name, event, members = team.groups()
    track = regex(r"for the SHISTECH (SDG track)", README_TEXT, "the track").group(1)
    contents = ["System at a glance", "Packet format", "Time on air", "Radio settings",
                "Node rules and forwarding", "Simulator setup", "Results", "Duty-cycle check",
                "Outside facts", "Wokwi prototype pins", "Known limits"]
    cover = f"""
<section class="cover">
  <p class="kicker">{esc(event)} · {esc(track)}</p>
  <h1>Emergency Mesh: Technical Reference</h1>
  <p class="team">Team {esc(team_name)}: {esc(members)}</p>
  <dl class="meta">
    <div><dt>Built</dt><dd>{today.day} {today:%B %Y}</dd></div>
    <div><dt>Commit</dt><dd><code>{esc(commit)}</code>{' + uncommitted changes' if dirty else ''}</dd></div>
    <div><dt>Generator</dt><dd><code>docs/tools/build_technical_reference.py</code></dd></div>
  </dl>
  <p class="cover-note">Every number in this document is read from the repository (code, results and
  README) or computed from it by the generator. Facts from outside sources are listed in section 10.
  Where text is quoted from the README, the section numbers in it are README sections.</p>
  <ol class="toc">{''.join(f'<li><span>{i}</span>{esc(t)}</li>' for i, t in enumerate(contents, 2))}</ol>
</section>"""

    # ------------------------------------------------- 2. system at a glance
    _caption, status_head, status_rows = tables(section("## Project status"))[0]
    status = simple_table([md_inline(h) for h in status_head],
                          [[md_inline(c) for c in r] for r in status_rows], "data status", "status")
    s2 = f"""
<section><h2><span>2</span>System at a glance</h2>
<p>A person presses a button on their home unit. The SOS goes over ESP-NOW to the nearest relay node,
then relay to relay over LoRa to the rescue base. The base confirms with an ACK that travels back the same way.</p>
{system_svg()}
<p class="src">Path of one SOS: home unit → nearest relay → other relays → rescue base → laptop. LoRa between relays at
SF{sf}, {bw / 1000:g} kHz (<code>RadioConfig</code>); every relay repeats each new message once.</p>
<h3>What is built and what is designed only</h3>
{status}
<p class="src">From the README, "Project status". Section numbers in this table are README sections.</p>
</section>"""

    # ------------------------------------------------------ 3. packet format
    sos_fields = packet_fields(lay, "SOS_BODY", with_landmark=True)
    ack_fields = packet_fields(lay, "ACK_BODY")
    hb_fields = packet_fields(lay, "HEARTBEAT_BODY")
    need(sos_fields[-1][1] + sos_fields[-1][2] == size_sos_max, "SOS byte map does not end at MAX_SOS_BYTES")
    meaning = {c[0].strip("`"): c for c in table_with(section("## 5."), "")[2]}
    extra_meaning = {
        "landmark_length": "Number of landmark bytes that follow",
        "ref_origin": "<code>origin</code> of the SOS being confirmed",
        "ref_seq": "<code>seq</code> of the SOS being confirmed",
        "battery_mv": "Battery voltage in millivolts",
        "uptime_s": "Seconds since the node started",
        "neighbours": "Nodes within radio range",
    }

    def offset_rows(fields):
        rows = []
        for name, off, size, code, group in fields:
            span = f"{off}" if size == 1 else f"{off}–{off + size - 1}"
            kind = "UTF-8 text" if code == "s" else CTYPE[code] + (", little-endian" if size > 1 else "")
            size_txt = f"0–{size} B" if code == "s" else f"{size} B"
            why = md_inline(meaning[name][2]) if name in meaning else extra_meaning.get(name, "")
            need(why, f"no meaning for field {name}")
            rows.append([span, f"<code>{name}</code>", size_txt, kind, why])
        return rows

    def enum_rows(enum, src_class):
        src = inspect.getsource(src_class)
        notes = dict(re.findall(r"(\w+) = \d+\s*#\s*(.+)", src))
        rows = [[str(int(m)), f"<code>{m.name}</code>", esc(notes.get(m.name, ""))] for m in enum]
        return rows if any(r[2] for r in rows) else [r[:2] for r in rows]

    flag_rows = [[str(int(f)), f"bit {int(f).bit_length() - 1}", f"<code>{f.name}</code>"] for f in Flag if f]
    hex_rows = []
    pkt_values = {"type": f"{first.type.value} ({first.type.name})", "origin": str(first.origin),
                  "seq": str(first.seq), "attempt": str(first.attempt), "hop_limit": str(first.hop_limit),
                  "hop_count": str(first.hop_count), "priority": f"{int(first.priority)} ({first.priority.name})",
                  "emergency_type": f"{first.emergency_type} ({EmergencyType(first.emergency_type).name})",
                  "people": str(first.people),
                  "flags": f"{first.flags} ({'|'.join(f.name for f in Flag if f and first.flags & f)})",
                  "session": str(first.session), "landmark_length": str(len(first.landmark.encode())),
                  "landmark": f'"{esc(first.landmark)}"'}
    for name, off, size, code, group in sos_fields:
        size = len(raw) - off if code == "s" else size
        chunk = raw[off:off + size]
        span = f"{off}" if size == 1 else f"{off}–{off + size - 1}"
        hex_rows.append([span, f"<code>{name}</code>", f"<code>{chunk.hex(' ')}</code>", pkt_values[name]])
    dump = " ".join(f"{b:02x}" for b in raw)
    s3 = f"""
<section class="newpage"><h2><span>3</span>Packet format</h2>
<p>All three packet types share a {hdr}-byte header. Multi-byte fields are little-endian
(<code>struct</code> format prefix <code>&lt;</code>), so the low byte is sent first. Layouts are read from
<code>protocol.py</code>.</p>
<h3>SOS packet, bytes 0–{size_sos_max - 1} ({size_sos_min}–{size_sos_max} bytes)</h3>
{bytemap_svg(sos_fields, size_sos_max)}
{legend(["header", "sos", "landmark"])}
{simple_table(["Bytes", "Field", "Size", "Type", "Meaning"], offset_rows(sos_fields), "data offsets", "sos-offsets")}
<p class="src">Header <code>struct.Struct("{esc(protocol.HEADER.format)}")</code> = {hdr} B; SOS body
<code>struct.Struct("{esc(protocol.SOS_BODY.format)}")</code> = {protocol.SOS_BODY.size} B; landmark ≤ {protocol.MAX_LANDMARK_BYTES} B
(<code>MAX_LANDMARK_BYTES</code>), cut so a UTF-8 character is never split. Meanings from README section 5.</p>
<div class="keep-block"><h3>ACK packet ({size_ack} bytes)</h3>
{bytemap_svg(ack_fields, size_ack)}
{legend(["header", "ack"])}
{simple_table(["Bytes", "Field", "Size", "Type", "Meaning"], offset_rows(ack_fields[len(lay["HEADER"]):]), "data offsets", "ack-offsets")}
<p class="src">Bytes 0–{hdr - 1} are the same header as the SOS packet.</p></div>
<div class="keep-block"><h3>Heartbeat packet ({size_hb} bytes)</h3>
{bytemap_svg(hb_fields, size_hb)}
{legend(["header", "hb"])}
{simple_table(["Bytes", "Field", "Size", "Type", "Meaning"], offset_rows(hb_fields[len(lay["HEADER"]):]), "data offsets", "hb-offsets")}</div>
<h3>Codes</h3>
<div class="cols">
<div>{simple_table(["Code", "<code>type</code>"], enum_rows(PacketType, PacketType), "data small enum", "enum-type")}
{simple_table(["Code", "<code>emergency_type</code>"], enum_rows(EmergencyType, EmergencyType), "data small enum", "enum-etype")}</div>
<div>{simple_table(["Code", "<code>priority</code>", "Meaning"], enum_rows(Priority, Priority)[::-1], "data small enum", "enum-prio")}
{simple_table(["Value", "Bit", "<code>flags</code>"], flag_rows, "data small enum", "enum-flags")}</div>
</div>
<p class="src">Flags combine by adding bits: <code>INJURED|TRAPPED</code> = {int(Flag.INJURED | Flag.TRAPPED)}.</p>
<div class="keep-block"><h3>Worked example: <code>encode()</code> of the first SOS in the rerouting demo</h3>
<p>The {first.priority.name.lower()} SOS from node {first.origin} in <code>run.py demo</code> (seed {seed}):
{EmergencyType(first.emergency_type).name.lower()}, {first.people} people, flags
{'|'.join(f.name for f in Flag if f and first.flags & f)}, landmark "{esc(first.landmark)}". It encodes to
{len(raw)} bytes:</p>
<pre class="hex">{dump}</pre>
{simple_table(["Bytes", "Field", "Hex", "Value"], hex_rows, "data keep", "hex-example")}
<p class="src">Built by calling <code>encode()</code> on the packet the simulator created; <code>decode()</code>
gives back the same packet. It matches row 1 of <code>results/demo_dashboard.csv</code>.</p></div>
</section>"""

    # --------------------------------------------------------- 4. time on air
    st31 = air_steps(size_sos_max, sf, bw)
    sizes = [("ACK", size_ack), ("SOS, no landmark", size_sos_min), ("Heartbeat", size_hb),
             ("SOS, longest landmark", size_sos_max)]
    size_rows = [[name, f"{n} B", f"{air_steps(n, sf, bw)['pay']:g}", ms(lora_airtime(n, sf, bw))]
                 for name, n in sizes]
    sf_rows = []
    for s in range(sf_lo, sf_hi + 1):
        a = air_steps(size_sos_max, s, bw)
        sf_rows.append([f"SF{s}", ms(a["t_sym"], 3), "on" if a["ldr"] else "off",
                        ms(lora_airtime(size_ack, s, bw)), ms(lora_airtime(size_sos_max, s, bw))])
    groups, prev = [], None
    for n in range(size_ack, size_sos_max + 1):
        t = lora_airtime(n, sf, bw)
        if prev is None or t != prev:
            groups.append([n, n, t])
        else:
            groups[-1][1] = n
        prev = t
    step_rows = [[f"{a}–{b} B" if a != b else f"{a} B", f"{air_steps(a, sf, bw)['pay']:g}", ms(t)]
                 for a, b, t in groups]
    block_bits = int(K["d4"]) * sf
    body_src = textwrap.dedent(AIRTIME_SRC.split('"""')[-1]).strip("\n")
    code_lines = body_src[body_src.index("t_sym ="):].split("\n")
    d = AIR_DEFAULTS
    s4 = f"""
<section class="newpage"><h2><span>4</span>Time on air</h2>
<p>Each packet's time on air comes from <code>lora_airtime()</code> in <code>protocol.py</code>, the SX127x formula
published by Semtech. Settings: SF{sf}, {bw / 1000:g} kHz, coding rate 4/{cr + int(K['cr4'])}, {d['preamble_symbols']}-symbol
preamble, CRC {'on' if d['crc'] else 'off'}, {'explicit' if d['explicit_header'] else 'implicit'} header.</p>
<h3>Our packets at SF{sf}</h3>
{simple_table(["Packet", "Size", "Payload symbols", "Time on air"], size_rows, "data", "air-sizes")}
<h3>The formula, as coded</h3>
<pre class="code">{esc(chr(10).join(code_lines))}</pre>
<h3>Worked example: a {size_sos_max}-byte SOS at SF{sf}</h3>
{simple_table(["Step", "Calculation", "Result"], [
        ["Symbol time", f"2<sup>{sf}</sup> / {bw:,} Hz", ms(st31['t_sym'], 3)],
        ["Low data rate optimisation", f"on only if a symbol is longer than {K['ldr_s'] * 1000:g} ms", "off" if not st31['ldr'] else "on"],
        ["Numerator", f"{K['n8']:g} × {size_sos_max} − {K['n4']:g} × {sf} + {K['n28']:g} + {K['ncrc']:g} × {int(d['crc'])} − {K['nih']:g} × {st31['ih']}", f"{st31['numer']:g}"],
        ["Blocks", f"⌈{st31['numer']:g} / ({K['d4']:g} × ({sf} − {K['d2']:g} × {st31['ldr']}))⌉ = ⌈{st31['numer'] / st31['denom']:.2f}⌉", str(st31['blocks'])],
        ["Payload symbols", f"{K['p8']:g} + {st31['blocks']} × ({cr} + {K['cr4']:g})", f"{st31['pay']:g}"],
        ["Preamble symbols", f"{d['preamble_symbols']} + {K['pre']:g}", f"{st31['pre']:g}"],
        ["Time on air", f"({st31['pre']:g} + {st31['pay']:g}) × {ms(st31['t_sym'], 3)}", f"<b>{ms(st31['total'])}</b>"],
    ], "data calc", "air-steps")}
<h3>Why several sizes take the same time</h3>
<p>The payload is sent in blocks: each block carries {block_bits} bits ({K['d4']:g} × SF{sf}) and costs
{cr + int(K['cr4'])} symbols, and a part-filled block costs the same as a full one. So time on air rises in steps:</p>
{simple_table(["Packet size", "Payload symbols", "Time on air"], step_rows, "data small", "air-steps-sizes")}
<p>The ACK ({size_ack} B) and heartbeat ({size_hb} B) fall in the same step as an SOS with no landmark ({size_sos_min} B).</p>
<h3>Spreading factor</h3>
{simple_table(["Spreading factor", "Symbol time", "Low data rate opt.", f"ACK ({size_ack} B)", f"SOS ({size_sos_max} B)"], sf_rows, "data", "air-sf")}
<p class="src">SF{sf_lo}–SF{sf_hi} is the range <code>run.py --sf</code> accepts; all results use SF{sf}.</p>
</section>"""

    # ------------------------------------------------------- 5. radio settings
    r_caption, r_head, r_rows = table_with(section("## 4."), "Radio settings")
    code_radio = [
        ["Spreading factor", f"SF{sf}", "<code>RadioConfig.spreading_factor</code>"],
        ["Bandwidth", f"{bw / 1000:g} kHz", "<code>RadioConfig.bandwidth_hz</code>"],
        ["Coding rate", f"4/{cr + int(K['cr4'])}", "<code>lora_airtime(coding_rate=…)</code>"],
        ["Preamble", f"{d['preamble_symbols']} symbols", "<code>lora_airtime(preamble_symbols=…)</code>"],
        ["Range", f"{radio.range_m:g} m", "<code>RadioConfig.range_m</code>"],
        ["Random packet loss", f"{radio.loss_probability * 100:g}%", "<code>RadioConfig.loss_probability</code>"],
    ]
    need(f"{radio.range_m:g} m range" in " ".join(r[0] for r in r_rows), "README range row disagrees with RadioConfig")
    need(any(f"SF{sf}" in r[0] for r in r_rows), "README SF row disagrees with RadioConfig")
    s5 = f"""
<section><h2><span>5</span>Radio settings</h2>
<p class="warn"><b>Assumed, not measured.</b> No radio has been tested in the field yet. The range and loss figures
come from published sources and our own allowances. README section 3.5 shows how much the results depend on them.</p>
<h3>In the code</h3>
{simple_table(["Setting", "Value", "Where"], code_radio, "data", "radio-code")}
<h3>Why these values (README section 4)</h3>
{simple_table([md_inline(h) for h in r_head], [[md_inline(c) for c in r] for r in r_rows], "data vr", "radio-readme")}
</section>"""

    # ------------------------------------------------ 6. node rules, forwarding
    p_caption, p_head, p_rows = table_with(section("## 4."), "Protocol settings")
    rules = [(["hop_limit"], "Hop limit"), (["seen_cache_size"], "Remember last"),
             (["ack_timeout_s", "max_attempts"], "Retry after"), (["jitter_max_s", "busy_backoff_s"], "Wait up to"),
             (["rate_limit_count", "rate_limit_window_s"], "SOS per"),
             (["heartbeat_interval_s", "silent_alert_s"], "Heartbeat")]
    covered, node_rows = set(), []
    for fields, key in rules:
        hits = [r for r in p_rows if key in r[0]]
        need(len(hits) == 1, f"README protocol row containing {key!r} not found")
        value_txt, reason = hits[0][0], hits[0][1]
        stated = numbers_in(value_txt)
        shown = []
        for f in fields:
            v = getattr(cfg, f)
            vals = list(v) if isinstance(v, tuple) else [v]
            for x in vals:
                forms = {float(x)} | ({float(x) / 60} if f.endswith("_s") and x >= 60 else set())
                need(forms & stated, f"NodeConfig.{f} = {x} is not what the README says ({value_txt!r})")
            shown.append(f"<code>{f} = {py_value(v)}</code>")
            covered.add(f)
        node_rows.append(["<br>".join(shown), md_inline(value_txt), md_inline(reason)])
    missing = set(NodeConfig.__dataclass_fields__) - covered
    need(not missing, f"NodeConfig fields without a README reason: {missing}")
    fwd = numbered_after(section("## 5."), "**Forwarding rules**")
    s6 = f"""
<section class="newpage"><h2><span>6</span>Node rules and forwarding</h2>
<p>Every <code>NodeConfig</code> field, its value in <code>node.py</code>, and the reason from README section 4.
The generator checks that each value matches the README.</p>
{simple_table(["In the code", "README", "Why"], node_rows, "data nodecfg", "nodeconfig")}
<h3>Forwarding rules (README section 5)</h3>
<ol class="rules">{''.join(f'<li>{md_inline(x)}</li>' for x in fwd)}</ol>
</section>"""

    # ------------------------------------------------------- 7. simulator setup
    n_caption, n_head, n_rows = table_with(section("## 4."), "Network layout")
    radio_model = [l.strip()[2:] for l in (network.__doc__ or "").splitlines() if l.strip().startswith("- ")]
    fsig = inspect.signature(scenarios.failures).parameters
    lsig = inspect.signature(scenarios.load).parameters
    hsig = inspect.signature(scenarios.health).parameters
    ssig = inspect.signature(scenarios.sensitivity).parameters
    f_window = int(regex(r"rng\.uniform\(0, (\d+)\), random_sos", inspect.getsource(scenarios.failures),
                         "failure test sending window").group(1))
    demo_src = inspect.getsource(scenarios.demo)
    demo_extra = int(regex(r"rng\.sample\(others, (\d+)\)", demo_src, "demo extra SOS").group(1))
    mix = ", ".join(f"{p.name.lower()} {share * 100:g}%" for p, share in scenarios.PRIORITY_MIX)

    def pcts(xs):
        return ", ".join(f"{x * 100:g}%" for x in xs)
    scen_rows = [
        ["Rerouting demo", f"A critical SOS from the farthest node plus {demo_extra} random SOS; the relay in the middle of "
                           "its route is then destroyed and a second SOS is sent from the same node.",
         f"seed {seed}"],
        ["Failures", f"Destroy {pcts(fsig['fractions'].default)} of relays at random, then every surviving node sends one SOS "
                     f"within {dur(f_window)}. Run for {dur(fsig['duration_s'].default)}.",
         f"{trials} networks per row, seed {seed}"],
        ["Load", f"{', '.join(map(str, lsig['loads'].default))} SOS from random nodes within {dur(lsig['window_s'].default)}, "
                 f"no nodes destroyed. Run for {dur(lsig['duration_s'].default)}. Priority mix: {mix}.",
         f"{trials // load_div} networks per row (<code>--trials // {load_div}</code>), seed {seed}"],
        ["Health", f"Every relay sends a heartbeat every {dur(cfg.heartbeat_interval_s)}; {hsig['kill_count'].default} "
                   f"nodes die silently after {dur(hsig['kill_at_s'].default)}; the base checks at "
                   f"{dur(hsig['duration_s'].default)} and flags nodes silent for over {dur(cfg.silent_alert_s)}.",
         f"{trials // health_div} runs (<code>--trials // {health_div}</code>), seed {seed}"],
        ["Sensitivity", f"The failure test at ranges {', '.join(f'{r} m' for r in ssig['ranges'].default)} and random loss "
                        f"{pcts(ssig['losses'].default)}, with {pcts(ssig['fractions'].default)} of relays destroyed.",
         f"{trials} networks per row, seed {seed}"],
    ]
    s7 = f"""
<section><h2><span>7</span>Simulator setup</h2>
<p><code>grid_nodes()</code> places {g_rows} × {g_cols} = {len(nodes)} nodes, {spacing:g} m apart with a random offset of up to
±{jitter:g} m each way, over {(g_cols - 1) * spacing / 1000:g} km × {(g_rows - 1) * spacing / 1000:g} km. The centre point is the
rescue base, so there are <b>{len(relays)} relays + 1 base</b>.</p>
<p>In the seed-{seed} demo network, each relay has {min(degree)}–{max(degree)} neighbours within {radio.range_m:g} m
(mean {sum(degree) / len(degree):.1f}), and the farthest relay is {max_hops} hops from the base, inside the hop limit of {cfg.hop_limit}.</p>
<h3>Radio model (from <code>network.py</code>)</h3>
<ul>{''.join(f'<li>{md_inline(x)}</li>' for x in radio_model)}</ul>
<h3>Scenarios, as run by <code>python run.py all</code></h3>
{simple_table(["Scenario", "What happens", "Runs"], scen_rows, "data scen", "scenarios")}
<p class="src">Defaults read from <code>scenarios.py</code> and <code>run.py</code> (<code>--trials {trials}</code>, <code>--seed {seed}</code>).
The README reports that seeds {seeds_m.group(1)} and {seeds_m.group(2)} gave the same main conclusions.</p>
<h3>Why this layout (README section 4)</h3>
{simple_table([md_inline(h) for h in n_head], [[md_inline(c) for c in r] for r in n_rows], "data vr", "layout-readme")}
</section>"""

    # ---------------------------------------------------------------- 8. results
    s8 = f"""
<section class="newpage"><h2><span>8</span>Results</h2>
<p>Every CSV in <code>emergency-mesh/simulation/results/</code>, with every column. Column names are shown under each heading.</p>
<h3>Rerouting demo: rescue dashboard</h3>
{csv_table("demo_dashboard.csv", "small")}
{image("demo_topology.png", "the demo network, both SOS routes and the destroyed relay")}
<h3>Delivery as relays are destroyed</h3>
{csv_table("failures.csv", "small")}
{image("failures.png", "delivery as relays are destroyed")}
<h3>Many SOS at once</h3>
{csv_table("load.csv", "small")}
{image("load.png", "delivery and delay under load")}
<h3>Detecting dead nodes</h3>
{csv_table("health.csv")}
<h3>Sensitivity to the radio assumptions</h3>
{csv_table("sensitivity.csv")}
</section>"""

    # ------------------------------------------------------- 9. duty-cycle check
    band_row = [r for r in r_rows if "MHz band" in r[0]]
    need(len(band_row) == 1, "README band row not found")
    limit = float(regex(r"transmitting at most ([\d.]+)% of the time", band_row[0][1], "the duty-cycle limit").group(1))
    hb_air = lora_airtime(size_hb, sf, bw)
    per_hour = 3600 / cfg.heartbeat_interval_s
    hb_tx = len(relays) * per_hour
    hb_time = hb_tx * hb_air
    budget = limit / 100 * 3600
    left = budget - hb_time
    sos_air, ack_air = lora_airtime(size_sos_max, sf, bw), lora_airtime(size_ack, sf, bw)
    per_sos = sos_air + ack_air
    capacity = math.floor(left / per_sos)
    l_head, l_rows = read_csv("load.csv")
    big = max(int(r[0]) for r in l_rows)
    big_time = hb_time + big * per_sos
    duty_rows = [
        ["Heartbeat size and time on air", f"{size_hb} B at SF{sf}", ms(hb_air)],
        ["Heartbeats per relay per hour", f"3600 s / {cfg.heartbeat_interval_s:g} s", f"{per_hour:g}"],
        ["Heartbeats each relay sends per hour", f"{len(relays)} relays × {per_hour:g}: its own, plus each other relay's once",
         f"{hb_tx:g}"],
        ["Heartbeat time per relay per hour", f"{hb_tx:g} × {ms(hb_air)}", f"{hb_time:.1f} s = <b>{pct(hb_time / 36)}</b>"],
        ["Budget at the limit", f"{limit:g}% of 3600 s", f"{budget:g} s"],
        ["Left for SOS traffic", f"{budget:g} s − {hb_time:.1f} s", f"{left:.1f} s"],
        ["Time per SOS at each relay", f"SOS {size_sos_max} B ({ms(sos_air)}) + its ACK {size_ack} B ({ms(ack_air)})", ms(per_sos)],
        ["SOS per hour before the limit", f"{left:.1f} s / {ms(per_sos)}", f"<b>about {capacity}</b>, across the whole network"],
        [f"The {big}-SOS load test", f"{hb_time:.1f} s + {big} × {ms(per_sos)}",
         f"{big_time:.0f} s = <b>{pct(big_time / 36, 1)}</b> of the hour"],
    ]
    s9 = f"""
<section class="newpage"><h2><span>9</span>Duty-cycle check (our estimate)</h2>
<p class="warn"><b>Our own estimate, not a measurement.</b> Under India's rules for the {esc(band_row[0][0])}
(README section 4), each relay may transmit at most {limit:g}% of the time. We assume a 1-hour observation period.
Because every relay repeats every message, each relay's load is the whole network's traffic.</p>
{simple_table(["Quantity", "Calculation", "Result"], duty_rows, "data calc", "duty")}
<ul>
<li>Assumes each relay forwards each SOS and its ACK exactly once (duplicates are dropped), with no retries.
Retries in a real mass event only add to this.</li>
<li>Heartbeats alone use {pct(hb_time / 36)} of each relay's time. The {big}-SOS load test sends {big} SOS within
{dur(lsig['window_s'].default)}, about {big / capacity:.1f} times what fits in a whole hour under the limit.</li>
<li>Home units use ESP-NOW on 2.4 GHz, a different band, so they don't count against this limit.</li>
<li>Gradient routing (README section 10) would cut how many relays repeat each message.</li>
</ul>
</section>"""

    # ------------------------------------------------------- 10. outside facts
    with open(FACTS, newline="", encoding="utf-8") as f:
        facts = list(csv.DictReader(f))
    fact_rows = [[esc(r["fact"]), esc(r["source"]),
                  f'<a href="{esc(r["link"])}">{esc(r["link"])}</a>' if r["link"] else '<span class="muted">not linked yet</span>']
                 for r in facts]
    s10 = f"""
<section class="newpage"><h2><span>10</span>Outside facts</h2>
<p>Facts this design relies on that come from outside the repository. None of them were measured by us. The list lives in
<code>docs/tools/outside_facts.csv</code>.</p>
{simple_table(["Fact", "Source", "Link"], fact_rows, "data facts", "facts")}
</section>"""

    # ---------------------------------------------------------------- 11. Wokwi
    w_caption, w_head, w_rows = table_with(section("## 6."), "Wokwi")
    wsrc = WOKWI.read_text(encoding="utf-8")
    code_pins = {
        "SCL": regex(r"scl=Pin\((\d+)\)", wsrc, "SCL pin").group(1),
        "SDA": regex(r"sda=Pin\((\d+)\)", wsrc, "SDA pin").group(1),
        "button": regex(r"button = Pin\((\d+)", wsrc, "button pin").group(1),
        "red": regex(r"red = Pin\((\d+)", wsrc, "red pin").group(1),
        "green": regex(r"green = Pin\((\d+)", wsrc, "green pin").group(1),
        "blue": regex(r"blue = Pin\((\d+)", wsrc, "blue pin").group(1),
    }
    readme_pins = " ".join(r[1] for r in w_rows)
    need(all(re.search(rf"\b{p}\b", readme_pins) for p in code_pins.values()), "README Wokwi pins disagree with main.py")
    wokwi_url = regex(r"https://wokwi\.com/projects/\d+", README_TEXT, "the Wokwi link").group(0)
    pin_rows = [[md_inline(r[0]), md_inline(r[1])] for r in w_rows]
    s11 = f"""
<section><h2><span>11</span>Wokwi prototype pins</h2>
<p>The node interface prototype (OLED screen, SOS button, RGB LED) runs in the Wokwi ESP32 simulator:
<a href="{wokwi_url}">{wokwi_url}</a>. Pins from README section 6, checked against
<code>emergency-mesh/wokwi_node/main.py</code>.</p>
{simple_table([md_inline(h) for h in w_head], pin_rows, "data", "wokwi")}
<p class="src">In <code>main.py</code>: I2C SCL = GPIO {code_pins['SCL']}, SDA = GPIO {code_pins['SDA']}; button = GPIO
{code_pins['button']}; LED red / green / blue = GPIO {code_pins['red']} / {code_pins['green']} / {code_pins['blue']}.</p>
</section>"""

    # --------------------------------------------------------- 12. known limits
    lim = bullets(section("## 9."))
    need(lim, "README section 9 bullets not found")
    lim_html = "".join(f"<h3>{esc(k)}</h3><ul>{''.join(f'<li>{md_inline(x)}</li>' for x in v)}</ul>"
                       for k, v in lim.items())
    s12 = f"""
<section><h2><span>12</span>Known limits</h2>
{lim_html}
<h3>Not measured</h3>
<ul><li>Radio range (LoRa and ESP-NOW) and battery life have not been measured on real hardware.</li></ul>
<p class="src">From README section 9.</p>
</section>"""

    body = cover + s2 + s3 + s4 + s5 + s6 + s7 + s8 + s9 + s10 + s11 + s12
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Emergency Mesh: Technical Reference</title>
<style>{CSS}</style></head><body><main>{body}</main></body></html>"""
    need("—" not in page, "the document contains an em dash")
    return page


def system_svg() -> str:
    boxes = [("Home unit", "buttons, LCD"), ("Nearest relay", "LoRa node"), ("Other relays", "repeat once"),
             ("Rescue base", "sorts incidents"), ("Laptop", "incident list")]
    edges = ["ESP-NOW", "LoRa", "LoRa", "USB"]
    w, bw_, gap, y, h = 672, 107, 32, 34, 58
    parts = [f'<svg class="sysdiag" viewBox="0 0 {w} 150" width="{w}" height="150" xmlns="http://www.w3.org/2000/svg" role="img">']
    for i, (title, sub) in enumerate(boxes):
        x = 2 + i * (bw_ + gap)
        dark = title == "Rescue base"
        parts.append(f'<rect x="{x + 3}" y="{y + 3}" width="{bw_}" height="{h}" fill="#141414" opacity="0.18"/>')
        parts.append(f'<rect x="{x}" y="{y}" width="{bw_}" height="{h}" fill="{"#141414" if dark else "#fff"}" '
                     f'stroke="#141414" stroke-width="1.5"/>')
        col = "#fff" if dark else "#141414"
        parts.append(f'<text x="{x + bw_ / 2}" y="{y + 24}" class="sd-t" fill="{col}">{title}</text>')
        sub_col = "#d9d2c3" if dark else "#5e5546"
        parts.append(f'<text x="{x + bw_ / 2}" y="{y + 41}" class="sd-s" style="fill:{sub_col}">{sub}</text>')
        if i < len(edges):
            x1, x2 = x + bw_ + 2, x + bw_ + gap - 2
            name = edges[i]
            parts.append(f'<line x1="{x1}" y1="{y + h / 2}" x2="{x2 - 5}" y2="{y + h / 2}" stroke="#141414" stroke-width="1.5"/>')
            parts.append(f'<path d="M{x2 - 6},{y + h / 2 - 4} L{x2},{y + h / 2} L{x2 - 6},{y + h / 2 + 4} Z" fill="#141414"/>')
            parts.append(f'<text x="{(x1 + x2) / 2}" y="{y - 8}" class="sd-e">{name}</text>')
    lx = 2 + bw_ / 2
    rx = 2 + 3 * (bw_ + gap) + bw_ / 2
    yb = y + h + 22
    parts.append(f'<path d="M{rx},{y + h} V{yb} H{lx} V{y + h + 7}" fill="none" stroke="#1f9d57" stroke-width="1.6"/>')
    parts.append(f'<path d="M{lx - 4},{y + h + 8} L{lx},{y + h + 2} L{lx + 4},{y + h + 8} Z" fill="#1f9d57"/>')
    parts.append(f'<text x="{(lx + rx) / 2}" y="{yb + 14}" class="sd-c">ACK (confirmation) → back the same way; '
                 f'the home unit shows "Delivered to rescue base"</text>')
    parts.append("</svg>")
    return "".join(parts)


CSS = """
@page { size: A4; }
:root { --ink:#141414; --sun:#ffd43b; --muted:#5e5546; --line:#141414; --soft:#f6f2e8; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin:0; color:var(--ink); background:#fff; font: 9.6pt/1.45 "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
main { width:100%; }
code, pre, .bm-off, .bm-name { font-family: Consolas, "Cascadia Mono", "Courier New", monospace; }
code { font-size: 0.92em; background: var(--soft); padding: 0 0.2em; border-radius: 2px; }
a { color: #1d4ed8; text-decoration: none; overflow-wrap: anywhere; }
section { margin: 0 0 7mm; }
section.newpage { break-before: page; }
h2 { font-size: 16pt; font-weight: 800; margin: 0 0 3mm; padding-bottom: 2mm; border-bottom: 2px solid var(--ink);
     display:flex; align-items:center; gap: 3mm; break-after: avoid; break-inside: avoid; }
h2 span { display:inline-grid; place-items:center; min-width: 9mm; height: 9mm; background: var(--sun);
          border: 1.5px solid var(--ink); box-shadow: 2px 2px 0 var(--ink); font-size: 12pt; }
h3 { font-size: 10.8pt; font-weight: 700; margin: 5mm 0 2mm; break-after: avoid; }
p { margin: 0 0 2.5mm; }
ul, ol { margin: 0 0 2.5mm; padding-left: 5mm; }
li { margin-bottom: 1mm; }
table { width:100%; border-collapse: collapse; margin: 1mm 0 2mm; font-size: 8.8pt; }
table.keep, .keep-block { break-inside: avoid; }
tr { break-inside: avoid; }
th, td { border: 1px solid var(--line); padding: 1.2mm 1.6mm; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { background: var(--sun); font-weight: 700; overflow-wrap: normal; }
th span { display:block; font: 400 7pt Consolas, monospace; color: var(--muted); margin-top: 0.5mm; overflow-wrap: anywhere; }
table.small { font-size: 8pt; }
table.small th, table.small td { padding: 0.9mm 1.2mm; }
table.data.status td:nth-child(2) b { white-space: nowrap; }
table.calc td:last-child { white-space: nowrap; }
table.offsets th:nth-child(1), table.offsets td:nth-child(1) { width: 11%; white-space: nowrap; }
table.offsets td:nth-child(2) { width: 21%; white-space: nowrap; }
table.offsets td:nth-child(3) { width: 9%; white-space: nowrap; }
table.offsets td:nth-child(4) { width: 22%; }
table.status td:nth-child(1) { width: 47%; }
table.status td:nth-child(3) { width: 27%; }
table.enum td, table.enum th { white-space: nowrap; }
table.enum td:last-child { white-space: normal; }
table.enum td:first-child { width: 16%; }
table.nodecfg td:nth-child(1) { width: 31%; }
table.nodecfg td:nth-child(1) code { white-space: nowrap; }
table.nodecfg td:nth-child(2) { width: 21%; }
table.scen td:first-child { white-space: nowrap; }
table.scen td:last-child { width: 24%; }
table.vr td:first-child { width: 24%; }
table.facts td:first-child { width: 48%; }
table.facts td:last-child { width: 22%; font-size: 7.6pt; }
.src { font-size: 8pt; color: var(--muted); }
.muted { color: var(--muted); font-style: italic; }
.warn { background: #fff5c2; border: 1.5px solid var(--ink); box-shadow: 2px 2px 0 var(--ink); padding: 2mm 3mm; }
pre { background: var(--soft); border-left: 3px solid var(--ink); padding: 2mm 3mm; margin: 1mm 0 3mm;
      font-size: 8.4pt; white-space: pre-wrap; overflow-wrap: anywhere; break-inside: avoid; }
pre.hex { letter-spacing: 0.02em; }
.bytemap, .sysdiag { display:block; max-width:100%; height:auto; margin: 2mm 0 0; }
.bm-off { font-size: 7.5px; text-anchor: middle; fill: var(--muted); }
.bm-name { font-size: 9px; fill: var(--ink); }
.legend { display:flex; gap: 5mm; font-size: 8pt; color: var(--muted); margin: 0 0 2mm; }
.legend i { display:inline-block; width: 3.2mm; height: 3.2mm; border: 1px solid var(--ink); margin-right: 1.3mm; vertical-align: -0.5mm; }
.sd-t { font: 700 11.5px "Segoe UI", Arial, sans-serif; text-anchor: middle; }
.sd-s { font: 9.5px "Segoe UI", Arial, sans-serif; text-anchor: middle; fill: var(--muted); }
.sd-e { font: 700 9px Consolas, monospace; text-anchor: middle; letter-spacing: 0.03em; }
.sd-c { font: 9.5px "Segoe UI", Arial, sans-serif; text-anchor: middle; fill: #1f9d57; }
.cols { display:grid; grid-template-columns: 1fr 1fr; gap: 4mm; }
figure { margin: 2mm 0 4mm; break-inside: avoid; text-align: center; }
figure img { max-width: 100%; max-height: 88mm; border: 1px solid var(--ink); }
figcaption { font-size: 8pt; color: var(--muted); margin-top: 1mm; }
ol.rules li { margin-bottom: 1.2mm; }
.cover { height: 255mm; display:flex; flex-direction:column; break-after: page; padding-top: 18mm; }
.cover .kicker { font: 700 9pt Consolas, monospace; letter-spacing: 0.12em; text-transform: uppercase; }
.cover h1 { font-size: 34pt; line-height: 1.05; font-weight: 900; margin: 4mm 0 6mm; padding: 5mm 6mm;
            background: var(--sun); border: 2px solid var(--ink); box-shadow: 4px 4px 0 var(--ink); }
.cover .team { font-size: 12pt; font-weight: 600; margin-bottom: 8mm; }
.meta { display:grid; grid-template-columns: repeat(3, auto); justify-content: start; gap: 2mm 10mm; margin: 0 0 8mm; }
.meta dt { font: 700 7.5pt Consolas, monospace; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); }
.meta dd { margin: 0.5mm 0 0; font-size: 10pt; }
.cover-note { max-width: 135mm; color: var(--muted); }
.toc { list-style: none; padding: 0; margin: auto 0 0; columns: 2; column-gap: 10mm; border-top: 2px solid var(--ink); padding-top: 4mm; }
.toc li { margin: 0 0 1.6mm; break-inside: avoid; }
.toc span { display:inline-block; width: 7mm; font: 700 9pt Consolas, monospace; }
"""

FOOTER = """<div style="width:100%; padding:0 16mm; font:8px 'Segoe UI', Arial, sans-serif; color:#5e5546;
display:flex; justify-content:space-between;"><span>Emergency Mesh: Technical Reference · commit {commit}</span>
<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>"""


CHECK_JS = """() => {
  const limit = document.querySelector('main').clientWidth + 0.5;
  const wide = [];
  document.querySelectorAll('table, pre, svg, figure, img').forEach(el => {
    const w = Math.max(el.scrollWidth, el.getBoundingClientRect().width);
    if (w > limit) wide.push((el.dataset && el.dataset.name) || el.tagName.toLowerCase());
  });
  const text = document.body.innerText;
  return { width: limit, wide, glyphs: ['→', '×', '≤'].filter(g => !text.includes(g)) };
}"""


def render_pdf(page_html: str, out: Path, commit: str, check: bool) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BuildError("Playwright is not installed: pip install -r docs/tools/requirements.txt")
    margin = 16  # mm, left and right
    content_px = round((210 - 2 * margin) / 25.4 * 96)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:  # no Google Chrome: fall back to Playwright's own Chromium
            browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": content_px, "height": 1100})
        page.emulate_media(media="print")
        page.set_content(page_html, wait_until="load")
        if check:
            result = page.evaluate(CHECK_JS)
            need(not result["wide"], f"wider than the page: {result['wide']}")
            need(not result["glyphs"], f"missing characters: {result['glyphs']}")
            print(f"checked: nothing wider than {result['width']:.0f} px; arrow, times and less-or-equal signs present")
        kwargs = dict(path=str(out), format="A4", print_background=True, display_header_footer=True,
                      header_template="<div></div>", footer_template=FOOTER.format(commit=esc(commit)),
                      margin={"top": "14mm", "bottom": "16mm", "left": f"{margin}mm", "right": f"{margin}mm"})
        try:
            page.pdf(**kwargs, outline=True, tagged=True)
        except TypeError:  # older Playwright without PDF bookmarks
            page.pdf(**kwargs)
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="PDF to write")
    ap.add_argument("--html", type=Path, help="also save the HTML here")
    ap.add_argument("--no-check", action="store_true", help="skip the width and character checks")
    args = ap.parse_args()
    commit, dirty = git_state(args.out)
    page_html = build_html(dt.date.today(), commit, dirty)
    if args.html:
        args.html.write_text(page_html, encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    render_pdf(page_html, args.out, commit + (" + uncommitted changes" if dirty else ""), not args.no_check)
    print(f"wrote {args.out} (commit {commit}{', with uncommitted changes' if dirty else ''})")


if __name__ == "__main__":
    main()
