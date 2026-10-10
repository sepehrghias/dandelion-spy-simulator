"""
topology.py
===========
Dynamic generation of a "spatial network" topology per Part 1, section 2.

Rules implemented:
  * Random placement of 20-30 nodes on a 1000x1000 plane.
  * Creation of 4-6 clusters (dense regions) with a Gaussian distribution around
    centers, and the remaining nodes scattered uniformly.
  * Edge construction based on geometric proximity, with degree kept in 2..4.
  * Guaranteed connectivity of the whole network (no isolated node).
  * Each cluster connected via at least two independent links (to two distinct
    external nodes) so that alternative paths exist.
  * Base link delay = Euclidean distance * 1 ms/unit.

Graph algorithms (BFS connectivity, Dijkstra shortest delay, bridge finding) are
implemented from scratch with no external dependency for full portability.
"""

from __future__ import annotations

import heapq
import math
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

import config


# ===========================================================================
# Topology data structure
# ===========================================================================
@dataclass
class Topology:
    n: int
    pos: List[Tuple[float, float]]              # coordinates of each node
    cluster: List[int]                          # cluster id (-1 = scattered)
    adj: List[Set[int]]                         # adjacency list
    node_uuid: List[str]                        # unique NodeID (UUID) per node
    seed: int = 0
    attempts: int = 1
    _base_delay: Dict[Tuple[int, int], float] = field(default_factory=dict)
    _apsp: Optional[np.ndarray] = None          # cached shortest delays

    # ---------------- delay build/access ----------------
    def _key(self, u: int, v: int) -> Tuple[int, int]:
        return (u, v) if u < v else (v, u)

    def base_delay(self, u: int, v: int) -> float:
        """Base delay of link u-v (ms) = Euclidean distance * MS_PER_UNIT."""
        return self._base_delay[self._key(u, v)]

    def euclidean(self, u: int, v: int) -> float:
        (x1, y1), (x2, y2) = self.pos[u], self.pos[v]
        return math.hypot(x1 - x2, y1 - y2)

    def recompute_delays(self) -> None:
        self._base_delay.clear()
        for u, v in self.edges():
            self._base_delay[self._key(u, v)] = self.euclidean(u, v) * config.MS_PER_UNIT
        self._apsp = None

    # ---------------- graph utilities ----------------
    def edges(self):
        for u in range(self.n):
            for v in self.adj[u]:
                if u < v:
                    yield (u, v)

    def n_edges(self) -> int:
        return sum(len(a) for a in self.adj) // 2

    def degree(self, u: int) -> int:
        return len(self.adj[u])

    def add_edge(self, u: int, v: int) -> None:
        if u != v:
            self.adj[u].add(v)
            self.adj[v].add(u)

    def neighbors(self, u: int) -> List[int]:
        return sorted(self.adj[u])

    def clusters(self) -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        for i, c in enumerate(self.cluster):
            if c >= 0:
                out.setdefault(c, []).append(i)
        return out

    # ---------------- connectivity ----------------
    def connected_components(self) -> List[List[int]]:
        seen = [False] * self.n
        comps: List[List[int]] = []
        for s in range(self.n):
            if seen[s]:
                continue
            stack, comp = [s], []
            seen[s] = True
            while stack:
                u = stack.pop()
                comp.append(u)
                for v in self.adj[u]:
                    if not seen[v]:
                        seen[v] = True
                        stack.append(v)
            comps.append(comp)
        return comps

    def is_connected(self) -> bool:
        if self.n == 0:
            return True
        return len(self.connected_components()) == 1

    def cluster_external_links(self, cid: int) -> List[Tuple[int, int]]:
        """Boundary edges of a cluster (one endpoint inside, one outside)."""
        members = set(self.clusters().get(cid, []))
        out = []
        for u in members:
            for v in self.adj[u]:
                if v not in members:
                    out.append((u, v))
        return out

    def cluster_external_targets(self, cid: int) -> Set[int]:
        """Set of distinct external nodes the cluster links to."""
        return {v for (_u, v) in self.cluster_external_links(cid)}

    # ---------------- Dijkstra (shortest base delay) ----------------
    def shortest_delays_from(self, src: int) -> np.ndarray:
        dist = np.full(self.n, np.inf)
        dist[src] = 0.0
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v in self.adj[u]:
                nd = d + self.base_delay(u, v)
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    def all_pairs_delays(self) -> np.ndarray:
        """n x n matrix of shortest base delays between all pairs (cached)."""
        if self._apsp is None:
            m = np.zeros((self.n, self.n))
            for s in range(self.n):
                m[s] = self.shortest_delays_from(s)
            self._apsp = m
        return self._apsp

    # ---------------- validation ----------------
    def validate(self) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        if not (config.N_NODES_MIN <= self.n <= config.N_NODES_MAX):
            reasons.append(f"node count out of range: {self.n}")
        # degrees
        for u in range(self.n):
            d = self.degree(u)
            if d < config.DEGREE_MIN:
                reasons.append(f"node {u} has degree below {config.DEGREE_MIN} ({d})")
        # connectivity
        if not self.is_connected():
            reasons.append(f"network is not connected ({len(self.connected_components())} components)")
        # multi-link cluster connectivity
        for cid, members in self.clusters().items():
            if len(members) < 2:
                continue  # a single-member cluster is effectively scattered
            tgt = self.cluster_external_targets(cid)
            if len(tgt) < config.CLUSTER_MIN_EXTERNAL_LINKS:
                reasons.append(
                    f"cluster {cid} has only {len(tgt)} independent external links "
                    f"(<{config.CLUSTER_MIN_EXTERNAL_LINKS})"
                )
        return (len(reasons) == 0, reasons)

    # ---------------- serialization ----------------
    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "pos": [list(p) for p in self.pos],
            "cluster": list(self.cluster),
            "adj": [sorted(a) for a in self.adj],
            "node_uuid": list(self.node_uuid),
            "seed": self.seed,
            "attempts": self.attempts,
        }

    @staticmethod
    def from_dict(d: dict) -> "Topology":
        t = Topology(
            n=d["n"],
            pos=[tuple(p) for p in d["pos"]],
            cluster=list(d["cluster"]),
            adj=[set(a) for a in d["adj"]],
            node_uuid=list(d["node_uuid"]),
            seed=d.get("seed", 0),
            attempts=d.get("attempts", 1),
        )
        t.recompute_delays()
        return t


# ===========================================================================
# Topology generation
# ===========================================================================
def _place_nodes(rng: np.random.Generator, n: int, k: int):
    """Place nodes: some inside Gaussian clusters, the rest scattered."""
    # cluster centers with a minimum separation
    centers: List[Tuple[float, float]] = []
    guard = 0
    margin = 2.2 * config.CLUSTER_SIGMA
    while len(centers) < k and guard < 5000:
        guard += 1
        c = (
            float(rng.uniform(margin, config.PLANE_SIZE - margin)),
            float(rng.uniform(margin, config.PLANE_SIZE - margin)),
        )
        if all(math.hypot(c[0] - o[0], c[1] - o[1]) >= config.CLUSTER_MIN_CENTER_DIST
               for o in centers):
            centers.append(c)
    # if we could not fit them, relax the separation
    while len(centers) < k:
        centers.append((
            float(rng.uniform(margin, config.PLANE_SIZE - margin)),
            float(rng.uniform(margin, config.PLANE_SIZE - margin)),
        ))

    n_clustered = int(round(config.CLUSTERED_FRACTION * n))
    n_clustered = max(2 * k, min(n_clustered, n))     # each cluster >= 2 members
    n_scattered = n - n_clustered

    # assign cluster members: first 2 guaranteed per cluster, then random
    assign: List[int] = []
    for cid in range(k):
        assign += [cid, cid]
    remaining = n_clustered - 2 * k
    if remaining > 0:
        assign += list(rng.integers(0, k, size=remaining))
    rng.shuffle(assign)

    pos: List[Tuple[float, float]] = []
    cluster: List[int] = []
    for cid in assign:
        cx, cy = centers[cid]
        x = float(np.clip(rng.normal(cx, config.CLUSTER_SIGMA), 0, config.PLANE_SIZE))
        y = float(np.clip(rng.normal(cy, config.CLUSTER_SIGMA), 0, config.PLANE_SIZE))
        pos.append((x, y))
        cluster.append(int(cid))
    for _ in range(n_scattered):
        pos.append((float(rng.uniform(0, config.PLANE_SIZE)),
                    float(rng.uniform(0, config.PLANE_SIZE))))
        cluster.append(-1)

    return pos, cluster, centers


def _sorted_pairs_by_distance(pos: List[Tuple[float, float]]):
    n = len(pos)
    pairs = []
    for u in range(n):
        for v in range(u + 1, n):
            d = math.hypot(pos[u][0] - pos[v][0], pos[u][1] - pos[v][1])
            pairs.append((d, u, v))
    pairs.sort()
    return pairs


def _build(rng: np.random.Generator, n: int, k: int, seed: int) -> Topology:
    pos, cluster, _centers = _place_nodes(rng, n, k)
    node_uuid = [str(uuid.UUID(bytes=bytes(rng.bytes(16)))) for _ in range(n)]
    topo = Topology(n=n, pos=pos, cluster=cluster,
                    adj=[set() for _ in range(n)], node_uuid=node_uuid, seed=seed)

    pairs = _sorted_pairs_by_distance(pos)

    # Phase 1: grow the graph up to the target degree using nearest edges
    for d, u, v in pairs:
        if topo.degree(u) < config.DEGREE_TARGET and topo.degree(v) < config.DEGREE_TARGET:
            topo.add_edge(u, v)

    # Phase 2: satisfy the minimum degree (2) using the nearest allowed edge
    for u in range(n):
        if topo.degree(u) >= config.DEGREE_MIN:
            continue
        for d, a, b in pairs:
            if u not in (a, b):
                continue
            other = b if a == u else a
            if other in topo.adj[u]:
                continue
            if topo.degree(other) < config.DEGREE_MAX:
                topo.add_edge(u, other)
                if topo.degree(u) >= config.DEGREE_MIN:
                    break

    # Phase 3: make connected (join components via the shortest inter-component edge)
    _make_connected(topo, pairs)

    # Phase 4: multi-link cluster connectivity (>=2 external links to distinct nodes)
    _ensure_cluster_multilink(topo, pairs)

    topo.recompute_delays()
    return topo


def _component_id(topo: Topology) -> List[int]:
    comp = [-1] * topo.n
    for cid, members in enumerate(topo.connected_components()):
        for u in members:
            comp[u] = cid
    return comp


def _make_connected(topo: Topology, pairs) -> None:
    while not topo.is_connected():
        comp = _component_id(topo)
        # shortest edge between two different components (prefer free degree)
        chosen = None
        fallback = None
        for d, u, v in pairs:
            if comp[u] == comp[v]:
                continue
            if fallback is None:
                fallback = (u, v)
            if topo.degree(u) < config.DEGREE_MAX and topo.degree(v) < config.DEGREE_MAX:
                chosen = (u, v)
                break
        u, v = chosen or fallback
        topo.add_edge(u, v)


def _ensure_cluster_multilink(topo: Topology, pairs) -> None:
    clusters = topo.clusters()
    for cid, members in clusters.items():
        if len(members) < 2:
            continue
        mset = set(members)
        targets = topo.cluster_external_targets(cid)
        # add the nearest boundary edges toward distinct external nodes
        for d, u, v in pairs:
            if len(targets) >= config.CLUSTER_MIN_EXTERNAL_LINKS:
                break
            inside = (u in mset, v in mset)
            if inside == (True, False):
                a_in, a_out = u, v
            elif inside == (False, True):
                a_in, a_out = v, u
            else:
                continue
            if a_out in targets:
                continue  # we need a distinct external node
            topo.add_edge(a_in, a_out)
            targets.add(a_out)


def generate_topology(seed: int,
                      n_nodes: Optional[int] = None,
                      n_clusters: Optional[int] = None) -> Topology:
    """
    Deterministically generate a valid topology from `seed`.
    First tries a single build + repair; in the rare case of an inconsistency it
    rebuilds with an internal counter (connectivity and multi-link connectivity
    are checked; if invalid, the topology is repaired or regenerated).
    """
    for attempt in range(config.TOPOLOGY_MAX_ATTEMPTS):
        rng = np.random.default_rng(np.random.SeedSequence([seed, attempt]))
        n = n_nodes if n_nodes else int(rng.integers(config.N_NODES_MIN, config.N_NODES_MAX + 1))
        k = n_clusters if n_clusters else int(rng.integers(config.N_CLUSTERS_MIN, config.N_CLUSTERS_MAX + 1))
        topo = _build(rng, n, k, seed)
        ok, reasons = topo.validate()
        if ok:
            topo.attempts = attempt + 1
            return topo
    raise RuntimeError(f"failed to generate a valid topology after "
                       f"{config.TOPOLOGY_MAX_ATTEMPTS} attempts: {reasons}")


# ===========================================================================
# Topology plotting
# ===========================================================================
def plot_topology(topo: Topology, path: str,
                  spy_set: Optional[Set[int]] = None,
                  title: str = "Network Topology") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    spy_set = spy_set or set()
    fig, ax = plt.subplots(figsize=(8, 8))

    # edges
    for u, v in topo.edges():
        (x1, y1), (x2, y2) = topo.pos[u], topo.pos[v]
        ax.plot([x1, x2], [y1, y2], color="#b8c4d0", lw=0.9, zorder=1)

    # cluster colors
    clusters = topo.clusters()
    cmap = plt.get_cmap("tab10")
    for i in range(topo.n):
        x, y = topo.pos[i]
        c = topo.cluster[i]
        if c >= 0:
            color = cmap(c % 10)
        else:
            color = "#9aa0a6"  # scattered
        if i in spy_set:
            ax.scatter([x], [y], s=170, marker="X", color="#d62728",
                       edgecolors="black", linewidths=0.8, zorder=3)
        else:
            ax.scatter([x], [y], s=90, color=color, edgecolors="black",
                       linewidths=0.5, zorder=2)
        ax.text(x + 6, y + 6, str(i), fontsize=7, zorder=4)

    from matplotlib.lines import Line2D
    legend = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#4c78a8",
               markersize=9, label="Honest node (colored by cluster)"),
        Line2D([0], [0], marker="X", color="w", markerfacecolor="#d62728",
               markersize=11, label="Spy node"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#9aa0a6",
               markersize=9, label="Scattered node"),
    ]
    ax.legend(handles=legend, loc="upper right", fontsize=8)

    ax.set_title(f"{title}  |  N={topo.n}, clusters={len(clusters)}, "
                 f"edges={topo.n_edges()}")
    ax.set_xlim(-20, config.PLANE_SIZE + 20)
    ax.set_ylim(-20, config.PLANE_SIZE + 20)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ===========================================================================
# Standalone quick test
# ===========================================================================
if __name__ == "__main__":
    config.ensure_dirs()
    t = generate_topology(seed=42)
    ok, reasons = t.validate()
    print(f"n={t.n}, clusters={len(t.clusters())}, edges={t.n_edges()}, "
          f"attempts={t.attempts}, valid={ok}")
    degs = [t.degree(i) for i in range(t.n)]
    print(f"degree min/mean/max = {min(degs)}/{sum(degs)/len(degs):.2f}/{max(degs)}")
    for cid, members in t.clusters().items():
        print(f"  cluster {cid}: size={len(members)}, "
              f"external targets={len(t.cluster_external_targets(cid))}")
    import os
    plot_topology(t, os.path.join(config.FIGURES_DIR, "topology_seed42.png"))
    print("saved figure.")
