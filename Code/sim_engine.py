"""
sim_engine.py
=============
Discrete-event simulation engine with a "logical clock" (milliseconds).

This engine is used for bulk statistics (parameter sweeps, multiple seeds, 200
packets per scenario) and is fully deterministic/reproducible: it has no OS
noise, and its output is a function of (topology, seed, configuration) only.

The protocol logic comes from protocol.py, so behavior matches the real UDP
runtime. The only difference is that delays are "simulated by an event queue"
instead of "real sleeps".

Output: a ScenarioLog containing
  * reference log: source and creation time of each packet,
  * coverage: first receipt time of each packet at each node (for T80%),
  * spy observations: (spy node, observed state, sender, time) per packet.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import config
import protocol
from protocol import FLUFF, STEM, NodeBehavior, Phase, ProtocolConfig
from topology import Topology


# ---------------------------------------------------------------------------
# Output structures
# ---------------------------------------------------------------------------
@dataclass
class SpyObservation:
    spy: int
    observed_state: str
    from_node: Optional[int]
    time_ms: float
    deliberate_delay_added: float = 0.0   # deliberate delay this spy added on rebroadcast


@dataclass
class PacketLog:
    packet_id: str
    source: int
    origin_time_ms: float
    # first receipt time of each node (includes the source at creation time)
    first_recv: Dict[int, float] = field(default_factory=dict)
    # first observation of each spy node
    spy_obs: List[SpyObservation] = field(default_factory=list)
    # stem length (number of Stem edges traversed before the first Fluff) -- Dandelion analysis
    stem_edges: int = 0
    fluff_point: Optional[int] = None     # node that first entered Fluff

    def coverage_count(self) -> int:
        return len(self.first_recv)

    def t_coverage(self, n_nodes: int, threshold: float) -> Optional[float]:
        """Time (ms since creation) for the packet to reach `threshold` of the nodes."""
        need = int(-(-threshold * n_nodes // 1))  # ceil
        times = sorted(self.first_recv.values())
        if len(times) < need:
            return None
        return times[need - 1] - self.origin_time_ms


@dataclass
class ScenarioLog:
    packets: List[PacketLog]
    n_nodes: int
    spy_set: Set[int]
    cfg: ProtocolConfig
    seed: int

    def coverage_fraction_mean(self) -> float:
        return sum(p.coverage_count() for p in self.packets) / (self.n_nodes * len(self.packets))


# ---------------------------------------------------------------------------
# Engine events
# ---------------------------------------------------------------------------
# event type: 0 = origin, 1 = deliver
_EV_ORIGIN = 0
_EV_DELIVER = 1


# ---------------------------------------------------------------------------
# Run a single scenario
# ---------------------------------------------------------------------------
def run_scenario(topo: Topology,
                 cfg: ProtocolConfig,
                 packets: List[Tuple[str, int, float]],
                 spy_set: Set[int],
                 seed: int,
                 spy_delay_policy: str = "none",
                 jitter_fraction: float = config.JITTER_FRACTION) -> ScenarioLog:
    """
    Run a full scenario on the discrete-event engine.

    packets: list of (packet_id, source_node, origin_time_ms)
    spy_set: set of spy node indices
    spy_delay_policy: deliberate-delay policy for spies (only matters in Phase 5)
    """
    rng = random.Random(seed)

    # each node has an independent, deterministic RNG (derived from the master seed)
    behaviors: List[NodeBehavior] = []
    for i in range(topo.n):
        node_rng = random.Random((seed << 8) ^ (i * 2654435761 & 0xFFFFFFFF))
        behaviors.append(NodeBehavior(i, topo.neighbors(i), cfg, node_rng))

    logs: Dict[str, PacketLog] = {}
    for pid, src, t0 in packets:
        logs[pid] = PacketLog(packet_id=pid, source=src, origin_time_ms=t0)
        logs[pid].first_recv[src] = t0        # the source holds the packet at creation time

    # event queue: (time, counter, type, data)
    heap: List[Tuple[float, int, int, tuple]] = []
    seq = 0

    def push(time_ms: float, etype: int, data: tuple):
        nonlocal seq
        heapq.heappush(heap, (time_ms, seq, etype, data))
        seq += 1

    for pid, src, t0 in packets:
        push(t0, _EV_ORIGIN, (pid, src))

    # process events in time order
    while heap:
        time_ms, _, etype, data = heapq.heappop(heap)

        if etype == _EV_ORIGIN:
            pid, src = data
            beh = behaviors[src]
            sends = beh.originate(pid)
            for s in sends:
                _schedule_send(topo, rng, time_ms, src, s.target, s.out_state,
                               spy_set, spy_delay_policy, jitter_fraction, push,
                               is_stem_edge=(s.out_state == STEM), plog=logs[pid])
            continue

        # _EV_DELIVER
        pid, out_state, from_node, to_node, stem_edges = data
        plog = logs[pid]
        beh = behaviors[to_node]
        was_new = pid not in beh.seen
        result = beh.on_receive(pid, out_state, from_node)

        if was_new:
            # first receipt at this node -> record coverage
            if to_node not in plog.first_recv:
                plog.first_recv[to_node] = time_ms
            # record spy observation (first observation of this spy node)
            if to_node in spy_set:
                plog.spy_obs.append(SpyObservation(
                    spy=to_node, observed_state=result.observed_state,
                    from_node=from_node, time_ms=time_ms))
            # record the fluff point and stem length (first time the packet fluffs)
            if (cfg.phase is Phase.DANDELION and plog.fluff_point is None
                    and out_state == STEM and result.observed_state == STEM
                    and any(s.out_state == FLUFF for s in result.sends)):
                plog.fluff_point = to_node
                plog.stem_edges = stem_edges
            # schedule rebroadcasts
            for s in result.sends:
                nxt_stem = stem_edges + (1 if s.out_state == STEM else 0)
                _schedule_send(topo, rng, time_ms, to_node, s.target, s.out_state,
                               spy_set, spy_delay_policy, jitter_fraction, push,
                               is_stem_edge=(s.out_state == STEM), plog=plog,
                               stem_edges_next=nxt_stem)

    return ScenarioLog(packets=list(logs.values()), n_nodes=topo.n,
                       spy_set=set(spy_set), cfg=cfg, seed=seed)


def _schedule_send(topo: Topology, rng: random.Random, now_ms: float,
                   sender: int, target: int, out_state: str,
                   spy_set: Set[int], spy_delay_policy: str,
                   jitter_fraction: float, push,
                   is_stem_edge: bool, plog: PacketLog,
                   stem_edges_next: Optional[int] = None) -> None:
    """Compute the link delay (+ spy deliberate delay) and schedule a deliver event."""
    base = topo.base_delay(sender, target)
    delay = protocol.transit_delay(base, rng, jitter_fraction)

    deliberate = 0.0
    if sender in spy_set and spy_delay_policy != "none":
        deliberate = protocol.spy_deliberate_delay(base, rng, spy_delay_policy)
        delay += deliberate

    arrival = now_ms + delay
    if stem_edges_next is None:
        stem_edges_next = 1 if is_stem_edge else 0
    push(arrival, _EV_DELIVER, (plog.packet_id, out_state, sender, target, stem_edges_next))


# ---------------------------------------------------------------------------
# Build the packet set of a scenario (200 random packets)
# ---------------------------------------------------------------------------
def make_packets(topo: Topology, spy_set: Set[int], seed: int,
                 n_packets: int = config.N_PACKETS,
                 window_ms: float = config.PACKET_INJECTION_WINDOW_MS
                 ) -> List[Tuple[str, int, float]]:
    """
    Build 200 random packets. Each packet's source is chosen uniformly at random
    from the "honest" nodes, and its creation time has no predetermined pattern.

    Note: packet sources and times are a function of (topology, spy_set, seed)
    only, so they stay fixed for a fair comparison across phases and p values.
    """
    rng = random.Random((seed << 16) ^ 0xA5A5)
    honest = [i for i in range(topo.n) if i not in spy_set]
    packets = []
    for k in range(n_packets):
        src = rng.choice(honest)
        t0 = rng.uniform(0.0, window_ms)
        pid = f"pkt-{seed}-{k:04d}"
        packets.append((pid, src, t0))
    return packets


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import topology
    config.ensure_dirs()
    topo = topology.generate_topology(seed=42)

    print(f"{'scenario':22s} {'cov':>6s} {'reach80':>8s} {'meanT80':>9s} "
          f"{'fluff%':>7s} {'stem|fluff':>10s}")
    for phase, p in [(Phase.DIFFUSION, 0.0), (Phase.DANDELION, 0.9),
                     (Phase.DANDELION, 0.5), (Phase.DANDELION, 0.1)]:
        cfg = ProtocolConfig(phase=phase, p=p)
        pkts = make_packets(topo, spy_set=set(), seed=1, n_packets=200)
        log = run_scenario(topo, cfg, pkts, spy_set=set(), seed=1)
        covs = [pl.coverage_count() / topo.n for pl in log.packets]
        t80 = [t for t in (pl.t_coverage(topo.n, 0.8) for pl in log.packets)
               if t is not None]
        reach80 = len(t80) / len(log.packets)
        avg_t80 = sum(t80) / len(t80) if t80 else float("nan")
        fluffed = [pl for pl in log.packets if pl.fluff_point is not None]
        fluff_rate = len(fluffed) / len(log.packets) if phase is Phase.DANDELION else 1.0
        stem_cond = (sum(pl.stem_edges for pl in fluffed) / len(fluffed)
                     if fluffed else 0.0)
        print(f"{phase.value+' p='+str(p):22s} {sum(covs)/len(covs):6.3f} "
              f"{reach80:8.2f} {avg_t80:8.1f}ms {fluff_rate:7.2f} {stem_cond:10.2f}")
