"""Charts for the simulation results. Requires matplotlib (optional)."""
from __future__ import annotations


def matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_topology(sim, first_path, second_path, killed, out_path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 7))
    nodes = sim.nodes
    drawn = set()
    for a, neighbours in sim._neighbours.items():
        for b in neighbours:
            if (b, a) in drawn:
                continue
            drawn.add((a, b))
            ax.plot([nodes[a].x, nodes[b].x], [nodes[a].y, nodes[b].y],
                    color="#cccccc", lw=0.8, zorder=1)

    def draw_path(path, colour, label, offset):
        xs = [nodes[i].x + offset for i in path]
        ys = [nodes[i].y + offset for i in path]
        ax.plot(xs, ys, color=colour, lw=3, label=label, zorder=2)

    if first_path:
        draw_path(first_path, "#d62728", "SOS 1 route", -15)
    if second_path:
        draw_path(second_path, "#1f77b4", "SOS 2 route (after relay destroyed)", 15)

    origin = first_path[0] if first_path else None
    for node in nodes.values():
        if node.is_base:
            ax.scatter(node.x, node.y, s=320, marker="*", color="#2ca02c", zorder=4,
                       label="Rescue base")
        elif node.id == killed:
            ax.scatter(node.x, node.y, s=160, marker="X", color="black", zorder=4,
                       label="Destroyed relay")
        else:
            is_origin = node.id == origin
            ax.scatter(node.x, node.y, s=90, color="#ff7f0e" if is_origin else "#7f7f7f",
                       zorder=3, label="SOS sender" if is_origin else None)
        ax.annotate(str(node.id), (node.x, node.y), textcoords="offset points",
                    xytext=(6, 6), fontsize=8)
    ax.set_title("Mesh topology: SOS rerouted around a destroyed relay")
    ax.set_xlabel("metres")
    ax.set_ylabel("metres")
    ax.set_aspect("equal")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3, fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_failures(rows, out_path) -> None:
    plt = _plt()
    x = [r["failed_pct"] for r in rows]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(x, [r["reachable_pct"] for r in rows], "o--", color="#7f7f7f",
            label="Physically reachable (upper limit)")
    ax.plot(x, [r["delivered_pct"] for r in rows], "o-", color="#d62728",
            label="Delivered to rescue base")
    ax.plot(x, [r["confirmed_pct"] for r in rows], "s:", color="#1f77b4",
            label="Sender got confirmation")
    ax.set_xlabel("% of relay nodes destroyed")
    ax.set_ylabel("% of SOS messages")
    ax.set_ylim(0, 105)
    ax.set_title("Delivery as nodes fail (simulation)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_load(rows, out_path) -> None:
    plt = _plt()
    labels = [str(r["sos_in_60s"]) for r in rows]
    series = [("CRITICAL", "median_delay_critical_s", "#d62728"),
              ("URGENT", "median_delay_urgent_s", "#ff7f0e"),
              ("SUPPLIES", "median_delay_supplies_s", "#1f77b4"),
              ("SAFE", "median_delay_safe_s", "#7f7f7f")]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
    width = 0.2
    for i, (name, key, colour) in enumerate(series):
        xs = [j + (i - 1.5) * width for j in range(len(rows))]
        ax1.bar(xs, [r[key] for r in rows], width, label=name, color=colour)
    ax1.set_xticks(range(len(rows)))
    ax1.set_xticklabels(labels)
    ax1.set_xlabel("SOS messages sent within one minute")
    ax1.set_ylabel("median delay to rescue base (s)")
    ax1.set_title("Priority under load")
    ax1.legend(fontsize=8)
    ax1.grid(axis="y", alpha=0.3)

    ax2.plot(labels, [r["delivered_pct"] for r in rows], "o-", color="#d62728",
             label="Delivered within 1 hour")
    ax2.plot(labels, [r["within_5min_pct"] for r in rows], "s--", color="#1f77b4",
             label="Delivered within 5 minutes")
    ax2.set_ylim(0, 105)
    ax2.set_xlabel("SOS messages sent within one minute")
    ax2.set_ylabel("% of SOS messages")
    ax2.set_title("Delivery under load")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)
    fig.suptitle("Load test (simulation, no nodes destroyed)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
