"""
experiments.py
==============
Runs all 5 operational phases, parameter sweeps, and produces CSV/plots for the
report.

All metrics are produced by the discrete-event engine (sim_engine), which is
deterministic and fast; the real UDP runtime is used in main.py (live demo) and
for cross-validation.

Fair-comparison principle (from the specification): for each "experiment seed",
one topology, one spy set and one packet-source set are built and kept fixed
across all phases and all p values; final statistics are reported over several
seeds.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

import config
import metrics
import topology
from adversary import Adversary, max_spies, select_spies, weighted_betweenness
from metrics import describe
from protocol import Phase, ProtocolConfig
from sim_engine import make_packets, run_scenario

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ===========================================================================
# One "experiment seed": topology + spies + packets (fixed across phases)
# ===========================================================================
@dataclass
class Scenario:
    seed: int
    topo: topology.Topology
    spy_set: set
    max_spy_set: set
    packets: List[Tuple[str, int, float]]


def build_scenario(seed: int, n_spies: Optional[int] = None,
                   n_packets: int = config.N_PACKETS) -> Scenario:
    topo = topology.generate_topology(seed=seed)
    ns_max = max_spies(topo)
    max_spy_set = set(select_spies(topo, ns_max, strategy="betweenness"))
    ns = ns_max if n_spies is None else min(n_spies, ns_max)
    spy_set = set(select_spies(topo, ns, strategy="betweenness"))
    # packet sources are chosen from nodes honest "even w.r.t. the maximum spy
    # set", so that sources stay honest throughout the spy-count sweep.
    packets = make_packets(topo, max_spy_set, seed=seed, n_packets=n_packets)
    return Scenario(seed, topo, spy_set, max_spy_set, packets)


# ===========================================================================
# Save helpers
# ===========================================================================
def _save_csv(path: str, rows: List[dict], header: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"    -> CSV: {os.path.relpath(path, config.PROJECT_DIR)}")


def _save_fig(fig, path: str) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"    -> figure: {os.path.relpath(path, config.PROJECT_DIR)}")


# ===========================================================================
# Phase 0: topology and connectivity proof
# ===========================================================================
def experiment_topology(seed: int = 1) -> Scenario:
    print("\n=== Phase 0: topology generation and connectivity check ===")
    sc = build_scenario(seed)
    t = sc.topo
    ok, reasons = t.validate()
    degs = [t.degree(i) for i in range(t.n)]
    print(f"  N={t.n}, clusters={len(t.clusters())}, edges={t.n_edges()}, "
          f"attempts={t.attempts}")
    print(f"  degree min/mean/max = {min(degs)}/{sum(degs)/len(degs):.2f}/{max(degs)}")
    print(f"  connected={t.is_connected()}  valid={ok}")
    for cid, members in sorted(t.clusters().items()):
        tgt = t.cluster_external_targets(cid)
        print(f"    cluster {cid}: size={len(members)}, external independent links={len(tgt)}")
    print(f"  spy set (betweenness, {len(sc.spy_set)} nodes) = {sorted(sc.spy_set)}")

    topology.plot_topology(t, os.path.join(config.FIGURES_DIR, "topology.png"),
                           spy_set=sc.spy_set,
                           title=f"Spatial Topology (seed={seed})")
    # save connectivity proof and cluster info
    rows = []
    for cid, members in sorted(t.clusters().items()):
        rows.append({
            "cluster": cid, "size": len(members),
            "external_independent_links": len(t.cluster_external_targets(cid)),
            "members": " ".join(map(str, sorted(members))),
        })
    _save_csv(os.path.join(config.RESULTS_DIR, "topology_clusters.csv"), rows,
              ["cluster", "size", "external_independent_links", "members"])
    return sc


# ===========================================================================
# Phase 1: Base Diffusion
# ===========================================================================
def experiment_phase1(seeds: List[int]) -> None:
    print("\n=== Phase 1: Base Diffusion ===")
    cfg = ProtocolConfig(phase=Phase.DIFFUSION)
    rows = []
    sample_log = None
    for seed in seeds:
        sc = build_scenario(seed)
        log = run_scenario(sc.topo, cfg, sc.packets, sc.spy_set, seed=seed)
        t80 = metrics.t80_seconds(log)
        rows.append({
            "seed": seed, "coverage": round(metrics.coverage_fraction(log), 4),
            "reach80": round(metrics.reach_rate(log), 4),
            "T80_sec": round(t80, 4) if t80 else float("nan"),
        })
        if sample_log is None:
            sample_log = (sc, log)
        print(f"  seed={seed}: coverage={metrics.coverage_fraction(log):.3f} "
              f"reach80={metrics.reach_rate(log):.2f} "
              f"T80={t80:.3f}s")
    _save_csv(os.path.join(config.RESULTS_DIR, "phase1_diffusion.csv"), rows,
              ["seed", "coverage", "reach80", "T80_sec"])
    _plot_coverage_curve(sample_log[0], sample_log[1],
                         os.path.join(config.FIGURES_DIR, "phase1_coverage_curve.png"),
                         "Phase 1 - Base Diffusion: network coverage over time")


def _plot_coverage_curve(sc: Scenario, log, path: str, title: str) -> None:
    """Coverage (% of nodes) over time for several sample packets."""
    fig, ax = plt.subplots(figsize=(7, 4.5))
    n = sc.topo.n
    for pl in log.packets[:40]:
        times = sorted(t - pl.origin_time_ms for t in pl.first_recv.values())
        ys = [(i + 1) / n * 100 for i in range(len(times))]
        ax.plot([t / 1000 for t in times], ys, color="#4c78a8", alpha=0.18, lw=1)
    ax.axhline(80, color="#d62728", ls="--", lw=1.2, label="80% threshold")
    ax.set_xlabel("time since packet origin (s)")
    ax.set_ylabel("network coverage (%)")
    ax.set_title(title)
    ax.legend()
    ax.grid(alpha=0.3)
    _save_fig(fig, path)


# ===========================================================================
# Phase 2: attack on diffusion + optimal spy count
# ===========================================================================
def experiment_phase2(seeds: List[int], n_mc: int = 120) -> int:
    print("\n=== Phase 2: attack on diffusion + spy-count sweep ===")
    cfg = ProtocolConfig(phase=Phase.DIFFUSION)

    # spy-count sweep (averaged over seeds) to find the optimal n (max Score_adv)
    per_n: Dict[int, Dict[str, list]] = {}
    for seed in seeds:
        sc = build_scenario(seed)
        ns_max = max_spies(sc.topo)
        order = select_spies(sc.topo, ns_max, strategy="betweenness")  # nested
        for n in range(1, ns_max + 1):
            spy_set = set(order[:n])
            log = run_scenario(sc.topo, cfg, sc.packets, spy_set, seed=seed)
            adv = Adversary(sc.topo, spy_set, cfg)
            acc_base = metrics.accuracy(adv.guess_baseline(log), log)
            acc_prop = metrics.accuracy(adv.guess_dandelion(log, n_mc=n_mc), log)
            d = per_n.setdefault(n, {"base": [], "prop": [], "sadv_base": [],
                                     "sadv_prop": []})
            d["base"].append(acc_base)
            d["prop"].append(acc_prop)
            d["sadv_base"].append(acc_base / n)
            d["sadv_prop"].append(acc_prop / n)
        print(f"  seed={seed} sweep complete (n=1..{ns_max})")

    rows = []
    for n in sorted(per_n):
        d = per_n[n]
        rows.append({
            "n_spies": n,
            "acc_baseline": round(float(np.mean(d["base"])), 4),
            "acc_proposed": round(float(np.mean(d["prop"])), 4),
            "score_adv_baseline": round(float(np.mean(d["sadv_base"])), 5),
            "score_adv_proposed": round(float(np.mean(d["sadv_prop"])), 5),
        })
    _save_csv(os.path.join(config.RESULTS_DIR, "phase2_spy_sweep.csv"), rows,
              ["n_spies", "acc_baseline", "acc_proposed",
               "score_adv_baseline", "score_adv_proposed"])

    n_star = max(rows, key=lambda r: r["score_adv_proposed"])["n_spies"]
    print(f"  optimal spy count (max proposed Score_adv): n* = {n_star}")

    # sweep plot
    ns_vals = [r["n_spies"] for r in rows]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
    ax1.plot(ns_vals, [r["acc_baseline"] for r in rows], "o-", label="baseline (first-spy)")
    ax1.plot(ns_vals, [r["acc_proposed"] for r in rows], "s-", label="proposed (MC-Bayes)")
    ax1.set_xlabel("number of spy nodes")
    ax1.set_ylabel("source-detection accuracy")
    ax1.set_title("Phase 2 - accuracy vs #spies")
    ax1.legend(); ax1.grid(alpha=0.3)
    ax2.plot(ns_vals, [r["score_adv_baseline"] for r in rows], "o-", label="baseline")
    ax2.plot(ns_vals, [r["score_adv_proposed"] for r in rows], "s-", label="proposed")
    ax2.axvline(n_star, color="#d62728", ls="--", label=f"n* = {n_star}")
    ax2.set_xlabel("number of spy nodes")
    ax2.set_ylabel("Score_adv = accuracy / #spies")
    ax2.set_title("Phase 2 - adversary score vs #spies")
    ax2.legend(); ax2.grid(alpha=0.3)
    _save_fig(fig, os.path.join(config.FIGURES_DIR, "phase2_spy_sweep.png"))
    return n_star


# ===========================================================================
# Phase 3: Dandelion protocol and p parameter sweep (network behavior)
# ===========================================================================
def experiment_phase3(seeds: List[int]) -> None:
    print("\n=== Phase 3: Dandelion protocol and p parameter sweep (network behavior) ===")
    rows = []
    agg: Dict[float, Dict[str, list]] = {p: {"t80": [], "reach": [], "cov": [],
                                             "stem": []} for p in config.P_VALUES}
    for p in config.P_VALUES:
        cfg = ProtocolConfig(phase=Phase.DANDELION, p=p)
        for seed in seeds:
            sc = build_scenario(seed)
            log = run_scenario(sc.topo, cfg, sc.packets, sc.spy_set, seed=seed)
            t80 = metrics.t80_seconds(log)
            reach = metrics.reach_rate(log)
            cov = metrics.coverage_fraction(log)
            fluffed = [pl for pl in log.packets if pl.fluff_point is not None]
            stem = (sum(pl.stem_edges for pl in fluffed) / len(fluffed)
                    if fluffed else 0.0)
            agg[p]["t80"].append(t80 if t80 else float("nan"))
            agg[p]["reach"].append(reach)
            agg[p]["cov"].append(cov)
            agg[p]["stem"].append(stem)
            rows.append({"p": p, "seed": seed,
                         "T80_sec": round(t80, 4) if t80 else float("nan"),
                         "reach80": round(reach, 4), "coverage": round(cov, 4),
                         "mean_stem_len": round(stem, 3)})
        st = describe(agg[p]["t80"])
        sr = describe(agg[p]["reach"])
        print(f"  p={p}: T80 mean={st.mean:.3f}s median={st.median:.3f}s "
              f"std={st.std:.3f}  reach80 mean={sr.mean:.2f}")
    _save_csv(os.path.join(config.RESULTS_DIR, "phase3_dandelion_raw.csv"), rows,
              ["p", "seed", "T80_sec", "reach80", "coverage", "mean_stem_len"])

    # mean/median/std stats table
    stat_rows = []
    for p in config.P_VALUES:
        for metric in ("t80", "reach", "cov", "stem"):
            s = describe(agg[p][metric])
            stat_rows.append({"p": p, "metric": metric,
                              "mean": round(s.mean, 4), "median": round(s.median, 4),
                              "std": round(s.std, 4), "n": s.n})
    _save_csv(os.path.join(config.RESULTS_DIR, "phase3_dandelion_stats.csv"),
              stat_rows, ["p", "metric", "mean", "median", "std", "n"])

    _plot_phase3(agg, os.path.join(config.FIGURES_DIR, "phase3_psweep.png"))


def _plot_phase3(agg, path: str) -> None:
    ps = list(config.P_VALUES)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.3))
    for ax, key, ylabel, title in [
        (axes[0], "t80", "T80% (s)", "Coverage time vs p"),
        (axes[1], "reach", "reach-80% rate", "Reliability vs p"),
        (axes[2], "stem", "mean stem length", "Stem length vs p"),
    ]:
        means = [describe(agg[p][key]).mean for p in ps]
        stds = [describe(agg[p][key]).std for p in ps]
        ax.errorbar([str(p) for p in ps], means, yerr=stds, fmt="o-", capsize=4,
                    color="#4c78a8")
        ax.set_xlabel("p (stem-continue probability)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(alpha=0.3)
    fig.suptitle("Phase 3 - Dandelion parameter sweep (mean +/- std over seeds)")
    _save_fig(fig, path)


# ===========================================================================
# Phase 4: advanced attack on Dandelion (compare methods for three p)
# ===========================================================================
def experiment_phase4(seeds: List[int], n_mc: int = 160) -> None:
    print("\n=== Phase 4: advanced attack on Dandelion (three p values) ===")
    rows = []
    agg: Dict[float, Dict[str, list]] = {p: {"base": [], "tdoa": [], "prop": [],
                                             "sadv": []} for p in config.P_VALUES}
    for p in config.P_VALUES:
        cfg = ProtocolConfig(phase=Phase.DANDELION, p=p)
        for seed in seeds:
            sc = build_scenario(seed)
            log = run_scenario(sc.topo, cfg, sc.packets, sc.spy_set, seed=seed)
            adv = Adversary(sc.topo, sc.spy_set, cfg)
            acc_base = metrics.accuracy(adv.guess_baseline(log), log)
            acc_tdoa = metrics.accuracy(adv.guess_tdoa(log), log)
            acc_prop = metrics.accuracy(adv.guess_dandelion(log, n_mc=n_mc), log)
            n_bribed = len(sc.spy_set)
            agg[p]["base"].append(acc_base)
            agg[p]["tdoa"].append(acc_tdoa)
            agg[p]["prop"].append(acc_prop)
            agg[p]["sadv"].append(acc_prop / n_bribed)
            rows.append({"p": p, "seed": seed, "n_bribed": n_bribed,
                         "acc_baseline": round(acc_base, 4),
                         "acc_tdoa": round(acc_tdoa, 4),
                         "acc_proposed": round(acc_prop, 4),
                         "score_adv_proposed": round(acc_prop / n_bribed, 5)})
        sb, sp = describe(agg[p]["base"]), describe(agg[p]["prop"])
        print(f"  p={p}: baseline={sb.mean:.3f}  proposed={sp.mean:.3f} "
              f"(+/-{sp.std:.3f})  delta=+{sp.mean - sb.mean:.3f}")
    _save_csv(os.path.join(config.RESULTS_DIR, "phase4_attack_raw.csv"), rows,
              ["p", "seed", "n_bribed", "acc_baseline", "acc_tdoa",
               "acc_proposed", "score_adv_proposed"])

    stat_rows = []
    for p in config.P_VALUES:
        for method in ("base", "tdoa", "prop", "sadv"):
            s = describe(agg[p][method])
            stat_rows.append({"p": p, "method": method, "mean": round(s.mean, 4),
                              "median": round(s.median, 4), "std": round(s.std, 4)})
    _save_csv(os.path.join(config.RESULTS_DIR, "phase4_attack_stats.csv"), stat_rows,
              ["p", "method", "mean", "median", "std"])
    _plot_phase4(agg, os.path.join(config.FIGURES_DIR, "phase4_attack.png"))


def _plot_phase4(agg, path: str) -> None:
    ps = list(config.P_VALUES)
    x = np.arange(len(ps))
    w = 0.26
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, (key, label, color) in enumerate([
        ("base", "baseline (first-spy)", "#9aa0a6"),
        ("tdoa", "TDOA multilateration", "#f2a541"),
        ("prop", "proposed (MC-Bayes)", "#4c78a8"),
    ]):
        means = [describe(agg[p][key]).mean for p in ps]
        stds = [describe(agg[p][key]).std for p in ps]
        ax.bar(x + (i - 1) * w, means, w, yerr=stds, capsize=3, label=label, color=color)
    ax.set_xticks(x)
    ax.set_xticklabels([f"p={p}" for p in ps])
    ax.set_ylabel("source-detection accuracy")
    ax.set_title("Phase 4 - attack accuracy on Dandelion (mean +/- std)")
    ax.legend(); ax.grid(alpha=0.3, axis="y")
    _save_fig(fig, path)


# ===========================================================================
# Phase 5: deliberate spy delay and its effect on accuracy and T80%
# ===========================================================================
def experiment_phase5(seeds: List[int], n_mc: int = 160,
                      policies=("none", "max")) -> None:
    print("\n=== Phase 5: deliberate spy delay ===")
    rows = []
    agg: Dict[Tuple[float, str], Dict[str, list]] = {}
    for p in config.P_VALUES:
        cfg = ProtocolConfig(phase=Phase.DANDELION, p=p)
        for policy in policies:
            for seed in seeds:
                sc = build_scenario(seed)
                log = run_scenario(sc.topo, cfg, sc.packets, sc.spy_set, seed=seed,
                                   spy_delay_policy=policy)
                adv = Adversary(sc.topo, sc.spy_set, cfg)
                # the adversary knows its own delay policy, so it calibrates its
                # Monte Carlo model with the same policy (self-consistent).
                acc = metrics.accuracy(
                    adv.guess_dandelion(log, n_mc=n_mc, spy_delay_policy=policy), log)
                t80 = metrics.t80_seconds(log)
                sh = metrics.score_honest(t80, acc)
                key = (p, policy)
                d = agg.setdefault(key, {"acc": [], "t80": [], "sh": []})
                d["acc"].append(acc)
                d["t80"].append(t80 if t80 else float("nan"))
                d["sh"].append(sh)
                rows.append({"p": p, "policy": policy, "seed": seed,
                             "acc_proposed": round(acc, 4),
                             "T80_sec": round(t80, 4) if t80 else float("nan"),
                             "score_honest": round(sh, 5)})
            da = describe(agg[(p, policy)]["acc"])
            dt = describe(agg[(p, policy)]["t80"])
            print(f"  p={p} policy={policy:5s}: acc={da.mean:.3f}  T80={dt.mean:.3f}s")
    _save_csv(os.path.join(config.RESULTS_DIR, "phase5_delay_raw.csv"), rows,
              ["p", "policy", "seed", "acc_proposed", "T80_sec", "score_honest"])

    stat_rows = []
    for (p, policy), d in agg.items():
        for metric in ("acc", "t80", "sh"):
            s = describe(d[metric])
            stat_rows.append({"p": p, "policy": policy, "metric": metric,
                              "mean": round(s.mean, 5), "median": round(s.median, 5),
                              "std": round(s.std, 5)})
    _save_csv(os.path.join(config.RESULTS_DIR, "phase5_delay_stats.csv"), stat_rows,
              ["p", "policy", "metric", "mean", "median", "std"])
    _plot_phase5(agg, list(policies), os.path.join(config.FIGURES_DIR, "phase5_delay.png"))


def _plot_phase5(agg, policies, path: str) -> None:
    ps = list(config.P_VALUES)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
    for policy in policies:
        accs = [describe(agg[(p, policy)]["acc"]).mean for p in ps]
        t80s = [describe(agg[(p, policy)]["t80"]).mean for p in ps]
        ax1.plot([str(p) for p in ps], accs, "o-", label=f"delay={policy}")
        ax2.plot([str(p) for p in ps], t80s, "s-", label=f"delay={policy}")
    ax1.set_xlabel("p"); ax1.set_ylabel("adversary accuracy")
    ax1.set_title("Phase 5 - effect of spy delay on attack accuracy")
    ax1.legend(); ax1.grid(alpha=0.3)
    ax2.set_xlabel("p"); ax2.set_ylabel("T80% (s)")
    ax2.set_title("Phase 5 - effect of spy delay on coverage time")
    ax2.legend(); ax2.grid(alpha=0.3)
    _save_fig(fig, path)


# ===========================================================================
# Run all + final summary
# ===========================================================================
def run_all(quick: bool = False) -> None:
    config.ensure_dirs()
    if quick:
        seeds = [1, 2]
        n_packets = 60
        n_mc = 60
        print(">>> quick mode (for fast testing): 2 seeds, 60 packets, n_mc=60")
        _orig = config.N_PACKETS
        config.N_PACKETS = n_packets
    else:
        seeds = list(range(1, config.N_SEEDS + 1))
        n_mc = 160

    print("#" * 70)
    print("# Full simulation of 'Dandelion vs. Spy'")
    print(f"# experiment seeds: {seeds}")
    print("#" * 70)

    experiment_topology(seed=seeds[0])
    experiment_phase1(seeds)
    n_star = experiment_phase2(seeds, n_mc=max(60, n_mc - 40))
    experiment_phase3(seeds)
    experiment_phase4(seeds, n_mc=n_mc)
    experiment_phase5(seeds, n_mc=n_mc)

    _write_summary(seeds, n_star)

    if quick:
        config.N_PACKETS = _orig
    print("\n>>> done. Outputs saved in results/ and figures/.")


def _summary_row(phase_label, p, sc_list, cfg, n_mc):
    """One summary row: mean accuracy/T80/reach-rate over seeds + scores."""
    accs, t80s, reaches = [], [], []
    n_bribed = 0
    for sc in sc_list:
        log = run_scenario(sc.topo, cfg, sc.packets, sc.spy_set, seed=sc.seed)
        adv = Adversary(sc.topo, sc.spy_set, cfg)
        accs.append(metrics.accuracy(adv.guess_dandelion(log, n_mc=n_mc), log))
        t80s.append(metrics.t80_seconds(log))
        reaches.append(metrics.reach_rate(log))
        n_bribed = len(sc.spy_set)
    acc = float(np.mean(accs))
    t = float(np.nanmean([x for x in t80s if x]))
    reach = float(np.mean(reaches))
    sh = metrics.score_honest(t, acc)
    return {
        "phase": phase_label, "p": p, "n_bribed": n_bribed,
        "accuracy": round(acc, 4),
        "score_adv": round(acc / n_bribed, 5),
        "T80_sec": round(t, 4),
        "reach_rate": round(reach, 4),
        "score_honest": round(sh, 5),
        # effective score = Score_honest weighted by reach-rate, so that undelivered
        # packets at high p are not ignored (supplementary metric beyond the base
        # formula).
        "eff_score_honest": round(sh * reach, 5),
    }


def _write_summary(seeds: List[int], n_star=None) -> None:
    """Summary of Score_adv and Score_honest for all configurations."""
    print("\n=== final score summary ===")
    sc_list = [build_scenario(seed) for seed in seeds]
    rows = [_summary_row("2 (diffusion)", "-", sc_list,
                         ProtocolConfig(phase=Phase.DIFFUSION), n_mc=100)]
    for p in config.P_VALUES:
        rows.append(_summary_row("4 (dandelion)", p, sc_list,
                                 ProtocolConfig(phase=Phase.DANDELION, p=p), n_mc=140))

    _save_csv(os.path.join(config.RESULTS_DIR, "summary_scores.csv"), rows,
              ["phase", "p", "n_bribed", "accuracy", "score_adv", "T80_sec",
               "reach_rate", "score_honest", "eff_score_honest"])
    for r in rows:
        print(f"  {r['phase']:16s} p={str(r['p']):4s}  acc={r['accuracy']:.3f}  "
              f"Score_adv={r['score_adv']:.4f}  T80={r['T80_sec']:.3f}s  "
              f"reach={r['reach_rate']:.2f}  Score_honest={r['score_honest']:.4f}  "
              f"eff={r['eff_score_honest']:.4f}")


if __name__ == "__main__":
    import sys
    run_all(quick=("--quick" in sys.argv))
