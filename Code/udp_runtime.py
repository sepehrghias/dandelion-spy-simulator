"""
udp_runtime.py
==============
"Real" network runtime using UDP sockets.

This module runs the same protocol (protocol.py) over a real UDP network and
satisfies the project requirements:
  * Each node is a "logically independent process" running in its own thread with
    its own asyncio event loop (full concurrency: multi-threading + async I/O).
  * Each node operates on an independent UDP port on localhost.
  * Network-level packets contain only PacketID and state (Stem/Fluff); the real
    source id and creation time are kept only in the out-of-band "simulator
    reference log".
  * Link delay is simulated with "real sleeps" (times the time-scale factor).

The output is a ScenarioLog, so the same metrics and the same adversary as the
analysis engine can be applied to it (cross-validation of the two runtimes).
"""

from __future__ import annotations

import asyncio
import json
import queue
import random
import socket
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

import config
import protocol
from protocol import FLUFF, NodeBehavior, Phase, ProtocolConfig, STEM
from sim_engine import PacketLog, ScenarioLog, SpyObservation
from topology import Topology


# ---------------------------------------------------------------------------
# A node's datagram protocol
# ---------------------------------------------------------------------------
class _NodeProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: "UdpNode"):
        self.node = node

    def connection_made(self, transport):
        self.node.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.node.on_datagram(data, addr)


class UdpNode:
    """A real UDP node running in its own thread and asyncio event loop."""

    def __init__(self, index: int, uuid: str, port: int,
                 neighbor_addr: Dict[int, Tuple[str, int]],
                 base_delays: Dict[int, float],
                 behavior: NodeBehavior, is_spy: bool,
                 spy_delay_policy: str, time_scale: float,
                 jitter_fraction: float, report_q: "queue.Queue",
                 rng: random.Random):
        self.index = index
        self.uuid = uuid
        self.port = port
        self.neighbor_addr = neighbor_addr
        self.addr_to_idx = {v: k for k, v in neighbor_addr.items()}
        self.base_delays = base_delays
        self.behavior = behavior
        self.is_spy = is_spy
        self.spy_delay_policy = spy_delay_policy
        self.time_scale = time_scale
        self.jitter_fraction = jitter_fraction
        self.report_q = report_q
        self.rng = rng

        self.transport = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.bind_error: Optional[Exception] = None
        self._sock: Optional[socket.socket] = None
        self.t0 = 0.0                     # shared time origin (perf_counter)
        # Peer List: last-seen time of each neighbor (SelfAddr / last-seen)
        self.peer_last_seen: Dict[int, float] = {}

    # -------------------- logical time --------------------
    def now_ms(self) -> float:
        """Logical time (ms) since the shared network origin."""
        return (time.perf_counter() - self.t0) * 1000.0 / self.time_scale

    # -------------------- thread lifecycle --------------------
    def start(self, t0: float):
        self.t0 = t0
        self.thread = threading.Thread(target=self._run, name=f"node-{self.index}",
                                       daemon=True)
        self.thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self):
        # explicitly use SelectorEventLoop for UDP stability on Windows
        self.loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(self.loop)
        try:
            # manually create the socket with SO_REUSEADDR so consecutive runs
            # can re-bind the port
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((config.UDP_HOST, self.port))
            sock.setblocking(False)
            self._sock = sock
            coro = self.loop.create_datagram_endpoint(
                lambda: _NodeProtocol(self), sock=sock)
            self.transport, _ = self.loop.run_until_complete(coro)
        except Exception as e:
            self.bind_error = e
            self._ready.set()
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass
            self.loop.close()
            return
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            # NOTE: transport.close() defers closing the socket to a callback that
            # never runs after the loop is stopped; so we close the raw socket
            # directly to free the port immediately for subsequent runs.
            try:
                if self.transport:
                    self.transport.close()
            except Exception:
                pass
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass
            self.loop.close()

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

    # -------------------- datagram receipt --------------------
    def on_datagram(self, data: bytes, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            self._report(kind="error", packet_id="?", note="bad-datagram")
            return

        mtype = msg.get("t")
        pid = msg.get("id")

        if mtype == "O":
            # source injection by the controller: this node creates the packet
            self._originate(pid)
            return

        # data packet from a neighbor
        state = msg.get("s")
        from_node = self.addr_to_idx.get(tuple(addr))
        if from_node is not None:
            self.peer_last_seen[from_node] = self.now_ms()
        self._receive(pid, state, from_node)

    # -------------------- protocol logic --------------------
    def _originate(self, pid: str):
        t = self.now_ms()
        self._report(kind="origin", packet_id=pid, node=self.index, time_ms=t)
        sends = self.behavior.originate(pid)
        for s in sends:
            self._schedule_send(pid, s.target, s.out_state)

    def _receive(self, pid: str, state: str, from_node: Optional[int]):
        was_new = pid not in self.behavior.seen
        result = self.behavior.on_receive(pid, state, from_node)
        if not was_new:
            return  # duplicate: ignore (Seen Set)
        t = self.now_ms()
        self._report(kind="recv", packet_id=pid, node=self.index, time_ms=t,
                     from_node=from_node, state=result.observed_state,
                     is_spy=self.is_spy)
        for s in result.sends:
            self._schedule_send(pid, s.target, s.out_state)

    def _schedule_send(self, pid: str, target: int, out_state: str):
        base = self.base_delays[target]
        delay_ms = protocol.transit_delay(base, self.rng, self.jitter_fraction)
        if self.is_spy and self.spy_delay_policy != "none":
            delay_ms += protocol.spy_deliberate_delay(base, self.rng, self.spy_delay_policy)
        delay_sec = max(0.0, delay_ms * self.time_scale / 1000.0)
        self.loop.call_later(delay_sec, self._emit, pid, target, out_state)

    def _emit(self, pid: str, target: int, out_state: str):
        if not self.transport:
            return
        payload = json.dumps({"t": "D", "id": pid, "s": out_state}).encode("utf-8")
        try:
            self.transport.sendto(payload, self.neighbor_addr[target])
        except Exception:
            self._report(kind="error", packet_id=pid, note="send-failed")

    # -------------------- report to the reference log --------------------
    def _report(self, **ev):
        self.report_q.put(ev)


# ---------------------------------------------------------------------------
# Orchestration of the whole UDP network
# ---------------------------------------------------------------------------
class UdpNetwork:
    def __init__(self, topo: Topology, cfg: ProtocolConfig, spy_set: Set[int],
                 seed: int, spy_delay_policy: str = "none",
                 time_scale: float = config.UDP_TIME_SCALE,
                 base_port: int = config.UDP_BASE_PORT):
        self.topo = topo
        self.cfg = cfg
        self.spy_set = set(spy_set)
        self.seed = seed
        self.spy_delay_policy = spy_delay_policy
        self.time_scale = time_scale
        self.base_port = base_port
        self.report_q: "queue.Queue" = queue.Queue()
        self.nodes: List[UdpNode] = []
        self.ports = [base_port + i for i in range(topo.n)]

    def build(self):
        addr = {i: (config.UDP_HOST, self.ports[i]) for i in range(self.topo.n)}
        for i in range(self.topo.n):
            neigh = self.topo.neighbors(i)
            neighbor_addr = {j: addr[j] for j in neigh}
            base_delays = {j: self.topo.base_delay(i, j) for j in neigh}
            node_rng = random.Random((self.seed << 8) ^ (i * 2654435761 & 0xFFFFFFFF))
            beh = NodeBehavior(i, neigh, self.cfg, node_rng)
            self.nodes.append(UdpNode(
                index=i, uuid=self.topo.node_uuid[i], port=self.ports[i],
                neighbor_addr=neighbor_addr, base_delays=base_delays,
                behavior=beh, is_spy=(i in self.spy_set),
                spy_delay_policy=self.spy_delay_policy, time_scale=self.time_scale,
                jitter_fraction=config.JITTER_FRACTION, report_q=self.report_q,
                rng=random.Random((self.seed << 12) ^ (i * 40503))))

    def run(self, packets: List[Tuple[str, int, float]],
            verbose: bool = False) -> ScenarioLog:
        """
        Bring up the network, inject packets at their logical times, and after the
        network goes quiet, build a ScenarioLog.
        """
        with _HighResTimer():
            return self._run_inner(packets, verbose)

    def _run_inner(self, packets, verbose) -> ScenarioLog:
        t0 = time.perf_counter()
        for node in self.nodes:
            node.start(t0)

        # fast bind-error detection (instead of hanging in the collector)
        failed = [n.index for n in self.nodes if n.bind_error is not None]
        if failed:
            for node in self.nodes:
                node.stop()
            raise RuntimeError(
                f"bind failed for nodes {failed} on ports "
                f"{[self.ports[i] for i in failed]}; example error: "
                f"{self.nodes[failed[0]].bind_error}")

        # controller socket for source injection
        ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctrl.bind((config.UDP_HOST, config.UDP_CONTROLLER_PORT))

        # schedule injections (in logical-time order)
        injections = sorted(packets, key=lambda x: x[2])
        max_origin = injections[-1][2] if injections else 0.0

        def scale(ms: float) -> float:
            return ms * self.time_scale / 1000.0

        inj_thread = threading.Thread(
            target=self._inject_loop, args=(ctrl, injections, t0, scale, verbose),
            daemon=True)
        inj_thread.start()

        # collect reports until the network goes quiet
        events = self._collect(t0, scale(max_origin), verbose)
        inj_thread.join(timeout=1.0)

        # shut down the nodes
        for node in self.nodes:
            node.stop()
        for node in self.nodes:
            if node.thread:
                node.thread.join(timeout=2.0)
        ctrl.close()

        return self._build_log(packets, events)

    def _inject_loop(self, ctrl, injections, t0, scale, verbose):
        for pid, src, t_ms in injections:
            target_real = t0 + scale(t_ms)
            while True:
                dt = target_real - time.perf_counter()
                if dt <= 0:
                    break
                time.sleep(min(dt, 0.02))
            msg = json.dumps({"t": "O", "id": pid}).encode("utf-8")
            ctrl.sendto(msg, (config.UDP_HOST, self.ports[src]))
            if verbose:
                print(f"[inject] pkt={pid} src={src} at logical {t_ms:.0f}ms")

    def _collect(self, t0, last_inject_real, verbose):
        """Collect events until the network stays quiet for a while."""
        events = []
        # largest possible logical link delay times a few hops, as a quiet margin
        max_base = max((self.topo.base_delay(u, v) for u, v in self.topo.edges()),
                       default=100.0)
        quiet_logical_ms = (max_base * 2.5) * (2.0 if self.spy_delay_policy != "none" else 1.0)
        quiet_real = max(0.3, quiet_logical_ms * self.time_scale / 1000.0)
        hard_cap = time.perf_counter() + last_inject_real + 120.0

        last_event = time.perf_counter()
        injected_done_at = t0 + last_inject_real
        while True:
            try:
                ev = self.report_q.get(timeout=quiet_real)
                events.append(ev)
                last_event = time.perf_counter()
            except queue.Empty:
                now = time.perf_counter()
                # if all injections are done and nothing happened for a while: stop
                if now >= injected_done_at + quiet_real and (now - last_event) >= quiet_real:
                    break
                if now > hard_cap:
                    if verbose:
                        print("[collect] hard cap reached")
                    break
        return events

    def _build_log(self, packets, events) -> ScenarioLog:
        """Convert UDP events into a ScenarioLog compatible with metrics/adversary."""
        logs: Dict[str, PacketLog] = {}
        for pid, src, t in packets:
            logs[pid] = PacketLog(packet_id=pid, source=src, origin_time_ms=t)

        for ev in events:
            kind = ev.get("kind")
            pid = ev.get("packet_id")
            if pid not in logs:
                continue
            pl = logs[pid]
            if kind == "origin":
                pl.first_recv[ev["node"]] = ev["time_ms"]
            elif kind == "recv":
                node = ev["node"]
                if node not in pl.first_recv:
                    pl.first_recv[node] = ev["time_ms"]
                if ev.get("is_spy"):
                    pl.spy_obs.append(SpyObservation(
                        spy=node, observed_state=ev.get("state"),
                        from_node=ev.get("from_node"), time_ms=ev["time_ms"]))
        return ScenarioLog(packets=list(logs.values()), n_nodes=self.topo.n,
                           spy_set=set(self.spy_set), cfg=self.cfg, seed=self.seed)


class _HighResTimer:
    """
    Context manager that raises the Windows timer resolution to 1 ms
    (winmm.timeBeginPeriod). Without it, the default (~15.6 ms) resolution gets
    amplified by the time-scale factor and inflates T80%. It is a no-op on other
    operating systems.
    """
    def __enter__(self):
        self._winmm = None
        try:
            if _sys_is_windows():
                import ctypes
                self._winmm = ctypes.WinDLL("winmm")
                self._winmm.timeBeginPeriod(1)
        except Exception:
            self._winmm = None
        return self

    def __exit__(self, *exc):
        try:
            if self._winmm:
                self._winmm.timeEndPeriod(1)
        except Exception:
            pass


def _sys_is_windows() -> bool:
    import sys
    return sys.platform.startswith("win")


def run_udp_scenario(topo: Topology, cfg: ProtocolConfig,
                     packets: List[Tuple[str, int, float]], spy_set: Set[int],
                     seed: int, spy_delay_policy: str = "none",
                     time_scale: float = config.UDP_TIME_SCALE,
                     verbose: bool = False) -> ScenarioLog:
    net = UdpNetwork(topo, cfg, spy_set, seed, spy_delay_policy, time_scale)
    net.build()
    return net.run(packets, verbose=verbose)


# ---------------------------------------------------------------------------
# Quick test: real UDP run and comparison with the analysis engine
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import topology
    import metrics
    from sim_engine import make_packets, run_scenario
    from adversary import Adversary, select_spies, max_spies

    config.ensure_dirs()
    topo = topology.generate_topology(seed=42)
    ns = max_spies(topo)
    spies = set(select_spies(topo, ns))
    cfg = ProtocolConfig(phase=Phase.DANDELION, p=0.5)

    # small demo: 30 packets
    pkts = make_packets(topo, spies, seed=1, n_packets=30, window_ms=1500.0)

    print(f"Running real UDP on {topo.n} nodes / {topo.n} independent ports ...")
    print(f"ports: {config.UDP_BASE_PORT}..{config.UDP_BASE_PORT + topo.n - 1}")
    log_udp = run_udp_scenario(topo, cfg, pkts, spies, seed=1, verbose=False)
    log_sim = run_scenario(topo, cfg, pkts, spies, seed=1)

    for name, log in (("real UDP    ", log_udp), ("analysis eng", log_sim)):
        cov = metrics.coverage_fraction(log)
        t80 = metrics.t80_seconds(log)
        reach = metrics.reach_rate(log)
        adv = Adversary(topo, spies, cfg)
        acc = metrics.accuracy(adv.guess_baseline(log), log)
        t80s = f"{t80:.3f}s" if t80 is not None else "-"
        print(f"  {name}: coverage={cov:.3f} reach80={reach:.2f} "
              f"T80={t80s} baseline_acc={acc:.3f}")
