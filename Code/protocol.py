"""
protocol.py
===========
Pure node behavior logic (no network I/O whatsoever).

This module only makes "decisions" and is used by both runtimes:
  * the discrete-event engine (sim_engine.py) for bulk statistics,
  * the real UDP runtime (udp_runtime.py) for the live demo.

This guarantees the protocol behaves identically on both paths.

Implemented protocols
---------------------
1) Base Diffusion (Phase 1)
   After the first receipt, each node rebroadcasts the packet to all neighbors.

2) Dandelion (Phases 3-5)
   * The source always creates the packet in the Stem state and forwards it to a
     single random neighbor.
   * Each Stem receiver keeps the packet in Stem with probability p and forwards
     it to one random neighbor (other than the previous sender); with probability
     1-p (or if no eligible neighbor exists) the packet enters the Fluff state.
   * Once in Fluff, each node on first receipt rebroadcasts to all neighbors
     except the previous sender, and the state stays Fluff until the end.
   * Duplicate copies are ignored via the Seen Set.

Every network-level packet contains only the packet id (PacketID) and its state
(Stem/Fluff). The real source id and creation time are kept only in the
simulator reference log and never placed inside the packet payload.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Basic types
# ---------------------------------------------------------------------------
class Phase(Enum):
    DIFFUSION = "diffusion"   # phases 1 and 2
    DANDELION = "dandelion"   # phases 3, 4 and 5


# Packet state on the network level
STEM = "S"
FLUFF = "F"
# State is meaningless in plain diffusion; we reuse FLUFF for uniformity.
DIFFUSE = "F"


@dataclass(frozen=True)
class ProtocolConfig:
    phase: Phase
    p: float = 0.0            # probability of staying in Stem (Dandelion only)


@dataclass
class Send:
    """A decided send: forward the packet with out_state to node `target`."""
    target: int
    out_state: str


@dataclass
class ReceiveResult:
    duplicate: bool                 # was this a duplicate packet?
    observed_state: Optional[str]   # state this node "observed" (for spy logging)
    sends: List[Send]               # list of rebroadcasts


# ---------------------------------------------------------------------------
# Node behavior
# ---------------------------------------------------------------------------
class NodeBehavior:
    """
    Holds a node's local state and makes the protocol decisions.

    node_id     : node index
    neighbors   : list of neighbor indices
    cfg         : protocol configuration (phase and p)
    rng         : the node's local, deterministic RNG
    """

    def __init__(self, node_id: int, neighbors: List[int],
                 cfg: ProtocolConfig, rng: random.Random):
        self.node_id = node_id
        self.neighbors = list(neighbors)
        self.cfg = cfg
        self.rng = rng
        # Seen Set: the set of observed PacketIDs
        self.seen: set = set()

    # -------------------- packet creation by the source --------------------
    def originate(self, packet_id: str) -> List[Send]:
        """The source node creates a new packet."""
        self.seen.add(packet_id)
        if self.cfg.phase is Phase.DIFFUSION:
            # diffusion: forward to all neighbors
            return [Send(nb, DIFFUSE) for nb in self.neighbors]
        # Dandelion: always Stem, to a single random neighbor
        if not self.neighbors:
            return []
        nxt = self.rng.choice(self.neighbors)
        return [Send(nxt, STEM)]

    # -------------------- packet receipt --------------------
    def on_receive(self, packet_id: str, in_state: str,
                   from_node: Optional[int]) -> ReceiveResult:
        """Process a packet received from neighbor `from_node`."""
        # duplicate?
        if packet_id in self.seen:
            return ReceiveResult(duplicate=True, observed_state=in_state, sends=[])
        self.seen.add(packet_id)

        if self.cfg.phase is Phase.DIFFUSION:
            return self._diffusion_receive(in_state, from_node)
        return self._dandelion_receive(in_state, from_node)

    # -------------------- base diffusion (Phase 1) --------------------
    def _diffusion_receive(self, in_state: str,
                           from_node: Optional[int]) -> ReceiveResult:
        # After a new receipt, rebroadcast to all neighbors.
        # (Sending back to the previous sender is harmless -- it is dropped there
        #  as a duplicate -- so to reduce redundant traffic we exclude it.)
        sends = [Send(nb, DIFFUSE) for nb in self.neighbors if nb != from_node]
        return ReceiveResult(duplicate=False, observed_state=DIFFUSE, sends=sends)

    # -------------------- Dandelion (Phases 3-5) --------------------
    def _dandelion_receive(self, in_state: str,
                           from_node: Optional[int]) -> ReceiveResult:
        if in_state == FLUFF:
            # Fluff state is fixed until the end: forward to all but the sender
            sends = [Send(nb, FLUFF) for nb in self.neighbors if nb != from_node]
            return ReceiveResult(duplicate=False, observed_state=FLUFF, sends=sends)

        # Stem state
        eligible = [nb for nb in self.neighbors if nb != from_node]
        coin = self.rng.random()
        if coin < self.cfg.p and eligible:
            # stay in Stem: one random neighbor other than the previous sender
            nxt = self.rng.choice(eligible)
            return ReceiveResult(duplicate=False, observed_state=STEM,
                                 sends=[Send(nxt, STEM)])
        # enter Fluff (with probability 1-p or if no eligible neighbor exists).
        # This node "observed" the packet as Stem but rebroadcasts it as Fluff.
        sends = [Send(nb, FLUFF) for nb in eligible]
        return ReceiveResult(duplicate=False, observed_state=STEM, sends=sends)


# ---------------------------------------------------------------------------
# Delay helpers (shared by both runtimes)
# ---------------------------------------------------------------------------
def transit_delay(base: float, rng: random.Random,
                  jitter_fraction: float) -> float:
    """
    Final per-packet link delay:
        Delay_Total = Base + U(-f*Base, +f*Base)
    """
    j = rng.uniform(-jitter_fraction * base, jitter_fraction * base)
    return base + j


def spy_deliberate_delay(base: float, rng: random.Random, policy: str) -> float:
    """
    Deliberate delay a spy node adds before rebroadcast (Phase 5 only).
    Max allowed per send = base delay of the same link; unlimited holding is
    not allowed.

    policy:
      "none"   : no deliberate delay
      "max"    : always the maximum allowed (base)
      "random" : U(0, base)
      "half"   : half of the maximum (0.5*base)
    """
    if policy == "none":
        return 0.0
    if policy == "max":
        return base
    if policy == "half":
        return 0.5 * base
    if policy == "random":
        return rng.uniform(0.0, base)
    return 0.0
