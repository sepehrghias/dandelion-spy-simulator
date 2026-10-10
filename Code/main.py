"""
main.py
--------
Command-line interface for the "Dandelion vs. Spy" project.

Examples:
    python main.py topology --seed 1
    python main.py demo --p 0.5 --packets 30           # live UDP demo
    python main.py demo --phase diffusion --packets 20
    python main.py udp-validate                          # compare UDP vs analysis engine
    python main.py all                                   # run all 5 phases
    python main.py all --quick                           # fast run for testing
    python main.py phase3
"""

from __future__ import annotations

import argparse
import time

import config
import metrics
import topology
from adversary import Adversary, max_spies, select_spies
from protocol import Phase, ProtocolConfig
from sim_engine import make_packets, run_scenario


def cmd_topology(args):
    config.ensure_dirs()
    t = topology.generate_topology(seed=args.seed)
    ok, reasons = t.validate()
    degs = [t.degree(i) for i in range(t.n)]
    print(f"topology seed={args.seed}:")
    print(f"  N={t.n}  clusters={len(t.clusters())}  edges={t.n_edges()}  "
          f"attempts={t.attempts}")
    print(f"  degree min/mean/max = {min(degs)}/{sum(degs)/len(degs):.2f}/{max(degs)}")
    print(f"  connected={t.is_connected()}  valid={ok}")
    if not ok:
        print("  invalid reasons:", reasons)
    for cid, m in sorted(t.clusters().items()):
        print(f"    cluster {cid}: size={len(m)} external-independent-links="
              f"{len(t.cluster_external_targets(cid))}")
    ns = max_spies(t)
    spies = set(select_spies(t, ns))
    import os
    path = os.path.join(config.FIGURES_DIR, f"topology_seed{args.seed}.png")
    topology.plot_topology(t, path, spy_set=spies, title=f"Topology seed={args.seed}")
    print(f"  figure saved: {path}")


def cmd_demo(args):
    """Live UDP demo: real run on independent ports + adversary guesses."""
    from udp_runtime import run_udp_scenario
    config.ensure_dirs()

    t = topology.generate_topology(seed=args.seed)
    ns = max_spies(t)
    spies = set(select_spies(t, ns))
    phase = Phase.DIFFUSION if args.phase == "diffusion" else Phase.DANDELION
    cfg = ProtocolConfig(phase=phase, p=args.p)

    print("=" * 66)
    print("Live real UDP network demo  |  Dandelion vs. Spy")
    print("=" * 66)
    print(f"topology: N={t.n} nodes on {t.n} independent UDP ports "
          f"({config.UDP_BASE_PORT}..{config.UDP_BASE_PORT + t.n - 1})")
    print(f"protocol: {phase.value}" + (f"  p={args.p}" if phase is Phase.DANDELION else ""))
    print(f"spy nodes ({len(spies)}): {sorted(spies)}")
    print(f"sample NodeID (UUID) of node 0: {t.node_uuid[0]}")
    print(f"SelfAddr of node 0: {config.UDP_HOST}:{config.UDP_BASE_PORT}")
    print("-" * 66)

    pkts = make_packets(t, spies, seed=args.seed, n_packets=args.packets,
                        window_ms=args.packets * 60.0)
    print(f"running {args.packets} packets on the real UDP network "
          f"(time_scale={config.UDP_TIME_SCALE}) ...")
    t0 = time.perf_counter()
    log = run_udp_scenario(t, cfg, pkts, spies, seed=args.seed, verbose=args.verbose)
    dt = time.perf_counter() - t0
    print(f"finished in {dt:.1f} real seconds.\n")

    adv = Adversary(t, spies, cfg)
    g_base = adv.guess_baseline(log)
    g_prop = adv.guess_dandelion(log, n_mc=120, spy_delay_policy="none")

    print(f"{'packet':>10s} {'src':>4s} {'cover':>6s} {'T80(s)':>7s} "
          f"{'base':>5s} {'prop':>5s}")
    shown = 0
    for pl in log.packets:
        if shown >= args.show:
            break
        t80 = pl.t_coverage(t.n, 0.8)
        cov = pl.coverage_count() / t.n
        b = g_base.get(pl.packet_id)
        p = g_prop.get(pl.packet_id)
        bok = "OK" if b == pl.source else " x"
        pok = "OK" if p == pl.source else " x"
        t80s = f"{t80/1000:.2f}" if t80 else "  -"
        print(f"{pl.packet_id:>10s} {pl.source:>4d} {cov:6.2f} {t80s:>7s} "
              f"{str(b):>3s}{bok} {str(p):>3s}{pok}")
        shown += 1

    acc_b = metrics.accuracy(g_base, log)
    acc_p = metrics.accuracy(g_prop, log)
    t80 = metrics.t80_seconds(log)
    print("-" * 66)
    t80s = f"{t80:.3f}s" if t80 else "-"
    print(f"overall metrics ({len(log.packets)} packets):")
    print(f"  mean coverage = {metrics.coverage_fraction(log):.3f}   "
          f"reach-80% = {metrics.reach_rate(log):.2f}   T80% = {t80s}")
    print(f"  adversary accuracy:  baseline = {acc_b:.3f}   proposed = {acc_p:.3f}")
    print(f"  Score_adv (proposed) = {acc_p/len(spies):.4f}")
    print(f"  Score_honest = {metrics.score_honest(t80, acc_p):.4f}")


def cmd_udp_validate(args):
    """Cross-validation: run one scenario on both runtimes and compare."""
    from udp_runtime import run_udp_scenario
    config.ensure_dirs()
    t = topology.generate_topology(seed=args.seed)
    spies = set(select_spies(t, max_spies(t)))
    cfg = ProtocolConfig(phase=Phase.DANDELION, p=args.p)
    pkts = make_packets(t, spies, seed=args.seed, n_packets=args.packets,
                        window_ms=args.packets * 60.0)
    print("cross-validation UDP <-> analysis engine:")
    log_sim = run_scenario(t, cfg, pkts, spies, seed=args.seed)
    log_udp = run_udp_scenario(t, cfg, pkts, spies, seed=args.seed)
    for name, log in (("analysis eng", log_sim), ("real UDP    ", log_udp)):
        adv = Adversary(t, spies, cfg)
        t80 = metrics.t80_seconds(log)
        t80s = f"{t80:.3f}s" if t80 else "-"
        acc = metrics.accuracy(adv.guess_baseline(log), log)
        print(f"  {name}: coverage={metrics.coverage_fraction(log):.3f}  "
              f"reach80={metrics.reach_rate(log):.2f}  T80={t80s}  "
              f"baseline_acc={acc:.3f}")


def cmd_phase(args):
    import experiments
    config.ensure_dirs()
    seeds = list(range(1, config.N_SEEDS + 1)) if args.seeds is None else args.seeds
    if args.which == "1":
        experiments.experiment_phase1(seeds)
    elif args.which == "2":
        experiments.experiment_phase2(seeds)
    elif args.which == "3":
        experiments.experiment_phase3(seeds)
    elif args.which == "4":
        experiments.experiment_phase4(seeds)
    elif args.which == "5":
        experiments.experiment_phase5(seeds)


def cmd_all(args):
    import experiments
    experiments.run_all(quick=args.quick)


def build_parser():
    p = argparse.ArgumentParser(
        description="Dandelion vs. Spy - dynamic simulation and network privacy challenge")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("topology", help="generate and plot a topology")
    pt.add_argument("--seed", type=int, default=1)
    pt.set_defaults(func=cmd_topology)

    pd = sub.add_parser("demo", help="live real UDP network demo")
    pd.add_argument("--seed", type=int, default=1)
    pd.add_argument("--p", type=float, default=0.5)
    pd.add_argument("--phase", choices=["diffusion", "dandelion"], default="dandelion")
    pd.add_argument("--packets", type=int, default=30)
    pd.add_argument("--show", type=int, default=15, help="number of packets to display")
    pd.add_argument("--verbose", action="store_true")
    pd.set_defaults(func=cmd_demo)

    pv = sub.add_parser("udp-validate", help="compare UDP and analysis engine")
    pv.add_argument("--seed", type=int, default=1)
    pv.add_argument("--p", type=float, default=0.5)
    pv.add_argument("--packets", type=int, default=40)
    pv.set_defaults(func=cmd_udp_validate)

    pp = sub.add_parser("phase", help="run a specific phase")
    pp.add_argument("which", choices=["1", "2", "3", "4", "5"])
    pp.add_argument("--seeds", type=int, nargs="*", default=None)
    pp.set_defaults(func=cmd_phase)

    pa = sub.add_parser("all", help="run all 5 phases + summary")
    pa.add_argument("--quick", action="store_true")
    pa.set_defaults(func=cmd_all)

    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
