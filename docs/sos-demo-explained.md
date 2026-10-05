SHISTECH · SDG track · Demo guide

# Emergency Mesh: SOS Demo Explained

Oct 5, 2026 · Team LVISG

## What the project is

- **The problem:** disasters knock out mobile towers just when people need help. After Cyclone Michaung (Chennai, 2023), roughly 30% of the city's towers were still down.
- **The idea:** small ESP32 + LoRa radio boxes ("nodes") are installed in advance at places people already gather: schools, relief camps, water tanks.
- **For the person in trouble:** join the node's Wi-Fi with any phone (no app, SIM or internet), pick what's wrong, how many people, and a landmark. No phone? Press the node's SOS button.
- **Across the network:** the SOS hops node to node over long-range radio to a rescue base, which lists incidents by priority and sends a confirmation back.
- **Status:** the network logic is built and tested in a Python simulator and the web demo; the physical nodes are designed but not built yet.
- **Team:** Manit Bhasin, Divit Rastogi, Rajveer Kapoor (LVISG), for SHISTECH's SDG track (SDG 11.5 and 9.1).

## How the SOS demo works

**On screen:**

- A map of 25 nodes; the square **B** in the centre is the rescue base.
- Shaded circles: each node's 200 m phone Wi-Fi reach. Grey lines: 800 m radio links between nodes.
- A side panel with the SOS form (priority, emergency type, people, landmark), playback speed, and the rescue dashboard.

**When you click the map to send an SOS:**

1. A person dot appears where you clicked, and the SOS goes to the nearest working node within 200 m.
2. That node broadcasts it. Every node that hears it passes it on once and ignores repeats, so copies spread along every working path (red, orange, blue or grey dots, by priority).
3. The first copy to reach the base shows up on the dashboard: priority, type, people, node location, landmark and hop count.
4. The base sends a confirmation back (green dots). The person's dot gets a check mark: "Delivered to rescue base".
5. No confirmation within 90 s? The sender tries again, waiting twice as long each time, up to 5 tries, then gives up.

> **Kept simple on purpose:** the demo leaves out radio collisions and random packet loss so each hop is easy to follow. The Python simulator models them, and the project's results come from it.

## Try it

- **Open it:** [manit-bhasin.github.io/Shistech_Lvis/emergency-mesh/web_demo](https://manit-bhasin.github.io/Shistech_Lvis/emergency-mesh/web_demo/) in any browser. Offline: download the repository ZIP and double-click `emergency-mesh/web_demo/index.html`.
- **Reroute:** choose "Destroy or repair a node", click relays on the last route, then send again. The new SOS goes around them.
- **Out of reach:** click empty space far from nodes. You get "No working node within 200 m".
- **Retries:** destroy nodes 4, 9 and 10, set speed to 20× and send from node 5. It retries 4 times, then gives up.
- **A crowd:** press "Simulate a crowd (20 SOS)". Critical messages jump the queue and sit at the top of the dashboard.
- **Reset** clears everything; the status line under the buttons explains each step.
