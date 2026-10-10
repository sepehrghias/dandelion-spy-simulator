"""
adversary.py
============
Adversary model and packet-source estimation strategies.

What does the adversary know? (Part 1, section 3)
  * It knows the base delay of every link and the graph structure (topology).
  * It has no global view: it only sees packets that reach the spy nodes under
    its control (log.spy_obs).
  * For each packet it must guess exactly one node as the source.

Spy selection
  * Default: weighted betweenness centrality -- nodes on the most shortest paths
    observe packets earliest and from more directions.

Estimation strategies
  1) Baseline "first spy observer":
        source = the sender (predecessor) of the earliest spy observation.
  2) Phase 2 (Diffusion) -- TDOA multilateration:
        using known base delays and observation times at several spies, estimate
        the source via least squares (the unknown t0 cancels via time differences).
  3) Phase 4 (Dandelion) -- Monte-Carlo-calibrated Bayesian:
        since the stem path is hidden, the adversary uses its own knowledge
        (graph, p, spy set) to simulate thousands of Dandelion diffusions and
        learns, per candidate source, the distribution of the "observation
        signature" (first spy, its predecessor, state); then for a real packet it
        picks the maximum-likelihood source.
"""

from __future__ import annotations

import heapq
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

import config
from protocol import FLUFF, STEM, Phase, ProtocolConfig
from sim_engine import ScenarioLog, run_scenario
from topology import Topology


# ===========================================================================
# Weighted betweenness (Brandes with Dijkstra) and spy selection
# ===========================================================================
def weighted_betweenness(topo: Topology) -> np.ndarray:
    n = topo.n
    CB = np.zeros(n)
    eps = 1e-9
    for s in range(n):
        S: List[int] = []
        P: List[List[int]] = [[] for _ in range(n)]
        sigma = np.zeros(n)
        sigma[s] = 1.0
        dist = np.full(n, np.inf)
        dist[s] = 0.0
        done = [False] * n
        pq = [(0.0, s)]
        while pq:
            d, v = heapq.heappop(pq)
            if done[v]:
                continue
            done[v] = True
            S.append(v)
            for w in topo.adj[v]:
                nd = d + topo.base_delay(v, w)
                if nd < dist[w] - eps:
                    dist[w] = nd
                    sigma[w] = sigma[v]
                    P[w] = [v]
                    heapq.heappush(pq, (nd, w))
                elif abs(nd - dist[w]) <= eps:
                    sigma[w] += sigma[v]
                    P[w].append(v)
        delta = np.zeros(n)
        while S:
            w = S.pop()
            for v in P[w]:
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                CB[w] += delta[w]
    return CB


def select_spies(topo: Topology, n_spies: int, seed: int = 0,
                 strategy: str = "betweenness") -> List[int]:
    """
    Select the set of spy nodes. Sets are "nested" for consecutive n
    (top-n is subset of top-(n+1)) so the spy-count sweep is a fair comparison.
    """
    n_spies = max(0, min(n_spies, topo.n))
    if n_spies == 0:
        return []
    if strategy == "random":
        rng = random.Random(seed)
        return sorted(rng.sample(range(topo.n), n_spies))
    if strategy == "degree":
        key = [(topo.degree(i), i) for i in range(topo.n)]
    else:  # betweenness (default)
        cb = weighted_betweenness(topo)
        key = [(cb[i], topo.degree(i), -i) for i in range(topo.n)]
    order = sorted(range(topo.n), key=lambda i: key[i], reverse=True)
    return sorted(order[:n_spies])


def max_spies(topo: Topology) -> int:
    """Maximum number of allowed spies = 30% of all nodes (floor)."""
    return int(math.floor(config.MAX_BRIBE_FRACTION * topo.n))


# ===========================================================================
# Shared observation-extraction helper
# ===========================================================================
def _earliest_obs(pl) -> Optional[object]:
    if not pl.spy_obs:
        return None
    return min(pl.spy_obs, key=lambda o: o.time_ms)


# ===========================================================================
# Adversary
# ===========================================================================
class Adversary:
    def __init__(self, topo: Topology, spy_set: Set[int], cfg: ProtocolConfig):
        self.topo = topo
        self.spy_set = set(spy_set)
        self.cfg = cfg
        self.honest = [i for i in range(topo.n) if i not in self.spy_set]
        self._apsp = topo.all_pairs_delays()
        # per-source Monte Carlo model (cached, for Phase 4)
        self._source_models: Optional[Dict[int, dict]] = None

    # ------------------------------------------------------------------
    # 1) Baseline: first spy observer
    # ------------------------------------------------------------------
    def guess_baseline(self, log: ScenarioLog) -> Dict[str, Optional[int]]:
        out: Dict[str, Optional[int]] = {}
        for pl in log.packets:
            o = _earliest_obs(pl)
            if o is None:
                out[pl.packet_id] = None
            elif o.from_node is not None:
                out[pl.packet_id] = o.from_node
            else:
                out[pl.packet_id] = o.spy
        return out

    # ------------------------------------------------------------------
    # 2) Phase 2: TDOA multilateration (least squares)
    # ------------------------------------------------------------------
    def guess_tdoa(self, log: ScenarioLog) -> Dict[str, Optional[int]]:
        out: Dict[str, Optional[int]] = {}
        for pl in log.packets:
            out[pl.packet_id] = self._tdoa_single(pl)
        return out

    def _tdoa_single(self, pl) -> Optional[int]:
        obs = pl.spy_obs
        if not obs:
            return None
        # earliest observation time as reference (cancels the unknown t0)
        obs_sorted = sorted(obs, key=lambda o: o.time_ms)
        if len(obs_sorted) == 1:
            # single spy: fall back to guessing the predecessor
            o = obs_sorted[0]
            return o.from_node if o.from_node is not None else o.spy
        ref = obs_sorted[0]
        t_ref = ref.time_ms
        spies = [o.spy for o in obs_sorted]
        dt = np.array([o.time_ms - t_ref for o in obs_sorted])
        best_s, best_res = None, math.inf
        for s in self.honest:
            d_ref = self._apsp[s, ref.spy]
            pred = np.array([self._apsp[s, sp] - d_ref for sp in spies])
            res = float(np.sum((dt - pred) ** 2))
            # tie-break: closer to the earliest spy
            res_tot = res + 1e-6 * d_ref
            if res_tot < best_res:
                best_res, best_s = res_tot, s
        return best_s

    # ------------------------------------------------------------------
    # 3) Phase 4: Monte-Carlo-calibrated Bayesian
    # ------------------------------------------------------------------
    def _build_source_models(self, n_mc: int, base_seed: int,
                             spy_delay_policy: str = "none") -> None:
        """
        For each honest source, simulate n_mc Dandelion diffusions on the known
        graph and learn the distribution of the "adversary observation signature":
          pair_counts[(first_spy, predecessor)] and pred_counts[predecessor]
        plus a triple that also includes the observed state.
        """
        models: Dict[int, dict] = {}
        for s in self.honest:
            triple_counts: Counter = Counter()   # (first_spy, predecessor, state)
            pair_counts: Counter = Counter()      # (first_spy, predecessor)
            pred_counts: Counter = Counter()      # predecessor
            observed = 0
            rng_seed_base = (base_seed * 1_000_003) ^ (s * 97)
            for k in range(n_mc):
                pkt = [(f"mc-{s}-{k}", s, 0.0)]
                sublog = run_scenario(self.topo, self.cfg, pkt, self.spy_set,
                                      seed=rng_seed_base + k,
                                      spy_delay_policy=spy_delay_policy)
                pl = sublog.packets[0]
                o = _earliest_obs(pl)
                if o is None:
                    continue
                observed += 1
                pred = o.from_node if o.from_node is not None else o.spy
                triple_counts[(o.spy, pred, o.observed_state)] += 1
                pair_counts[(o.spy, pred)] += 1
                pred_counts[pred] += 1
            models[s] = {
                "triple": triple_counts,
                "pair": pair_counts,
                "pred": pred_counts,
                "observed": observed,
                "n_mc": n_mc,
            }
        self._source_models = models

    def guess_dandelion(self, log: ScenarioLog,
                        n_mc: int = 160, base_seed: int = 12345,
                        gamma: float = 0.0,
                        spy_delay_policy: str = "none") -> Dict[str, Optional[int]]:
        """
        The main proposed attack (identical for Phase 2 and Phase 4);
        Monte-Carlo-calibrated Bayesian:
          score(s) = log P(first_spy, predecessor, state | s)   [triple signature]
                     + 0.25 log P(predecessor | s)               [fallback]
                     - gamma * normalized TDOA residual(s)       [optional; off by default]

        The source with the highest score is chosen. The timing term (gamma>0) is
        kept as an ablation; in practice, because Dandelion routing is not shortest
        path, adding it did not help, so the default is 0.
        """
        if self._source_models is None:
            self._build_source_models(n_mc, base_seed, spy_delay_policy)
        models = self._source_models
        alpha = 0.5  # Laplace smoothing

        base_vals = [self.topo.base_delay(u, v) for u, v in self.topo.edges()]
        mean_base = (sum(base_vals) / len(base_vals)) if base_vals else 1.0
        t_scale = (config.JITTER_FRACTION * mean_base) ** 2 + 1e-9

        out: Dict[str, Optional[int]] = {}
        for pl in log.packets:
            o = _earliest_obs(pl)
            if o is None:
                out[pl.packet_id] = None
                continue
            pred = o.from_node if o.from_node is not None else o.spy
            key3 = (o.spy, pred, o.observed_state)

            obs_sorted = sorted(pl.spy_obs, key=lambda ob: ob.time_ms)
            ref = obs_sorted[0]
            dt = np.array([ob.time_ms - ref.time_ms for ob in obs_sorted])
            spies = [ob.spy for ob in obs_sorted]
            use_timing = gamma > 0.0 and len(obs_sorted) >= 2

            best_s, best_score = None, -math.inf
            for s in self.honest:
                m = models[s]
                denom = m["observed"] + alpha * self.topo.n
                p_trip = (m["triple"].get(key3, 0) + alpha) / denom
                p_pred = (m["pred"].get(pred, 0) + alpha) / denom
                score = math.log(p_trip) + 0.25 * math.log(p_pred)
                if use_timing:
                    d_ref = self._apsp[s, ref.spy]
                    pred_dt = np.array([self._apsp[s, sp] - d_ref for sp in spies])
                    resid = float(np.mean((dt - pred_dt) ** 2)) / t_scale
                    score -= gamma * resid
                if score > best_score:
                    best_score, best_s = score, s

            out[pl.packet_id] = best_s
        return out

    # ------------------------------------------------------------------
    # Strategy selector by phase
    # ------------------------------------------------------------------
    def guess(self, log: ScenarioLog, method: str) -> Dict[str, Optional[int]]:
        if method == "baseline":
            return self.guess_baseline(log)
        if method == "tdoa":
            return self.guess_tdoa(log)
        if method == "dandelion":
            return self.guess_dandelion(log)
        raise ValueError(f"invalid method: {method}")


# ===========================================================================
# Quick test
# ===========================================================================
if __name__ == "__main__":
    import topology
    from sim_engine import make_packets
    config.ensure_dirs()
    topo = topology.generate_topology(seed=42)
    ns = max_spies(topo)
    spies = set(select_spies(topo, ns, strategy="betweenness"))
    print(f"N={topo.n}, max_spies={ns}, spies={sorted(spies)}")

    # Phase 2: Diffusion, compare baseline and TDOA
    cfg = ProtocolConfig(phase=Phase.DIFFUSION)
    pkts = make_packets(topo, spies, seed=1)
    log = run_scenario(topo, cfg, pkts, spies, seed=1)
    adv = Adversary(topo, spies, cfg)
    import metrics
    for method in ("baseline", "tdoa"):
        g = adv.guess(log, method)
        acc = metrics.accuracy(g, log)
        print(f"  Diffusion {method:9s}: acc={acc:.3f}  score_adv={acc/ns:.4f}")

    # Phase 4: Dandelion, compare baseline/TDOA/Bayes
    for p in (0.9, 0.5, 0.1):
        cfg = ProtocolConfig(phase=Phase.DANDELION, p=p)
        pkts = make_packets(topo, spies, seed=1)
        log = run_scenario(topo, cfg, pkts, spies, seed=1)
        adv = Adversary(topo, spies, cfg)
        row = []
        for method in ("baseline", "tdoa", "dandelion"):
            g = adv.guess(log, method)
            acc = metrics.accuracy(g, log)
            row.append(f"{method}={acc:.3f}")
        print(f"  Dandelion p={p}: " + "  ".join(row))
