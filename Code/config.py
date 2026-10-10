"""
config.py
=========
Global configuration for the "Dandelion vs. Spy" project.

All numeric constants required by the project specification are centralized
here so that both the discrete-event simulation engine (sim_engine) and the
real UDP runtime (udp_runtime) share a single source of truth.
"""

from __future__ import annotations

import sys as _sys

# Ensure UTF-8 console output (Persian text may appear in the generated report).
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 1) Spatial plane and topology parameters (Part 1, section 2)
# ---------------------------------------------------------------------------
PLANE_SIZE: float = 1000.0          # 2D plane dimensions (1000 x 1000)

N_NODES_MIN: int = 20               # minimum total number of nodes
N_NODES_MAX: int = 30               # maximum total number of nodes

N_CLUSTERS_MIN: int = 4             # minimum number of clusters (dense regions)
N_CLUSTERS_MAX: int = 6             # maximum number of clusters

# Fraction of nodes concentrated inside clusters (the rest are scattered)
CLUSTERED_FRACTION: float = 0.78

# Standard deviation of each cluster's Gaussian around its center (plane units)
CLUSTER_SIGMA: float = 55.0

# Minimum separation between cluster centers to keep regions distinct
CLUSTER_MIN_CENTER_DIST: float = 260.0

# --- Edge-construction rules (preferred degree 2..4) ---
DEGREE_MIN: int = 2                 # minimum degree per node (no isolated node)
DEGREE_TARGET: int = 3              # target degree during graph growth
DEGREE_MAX: int = 4                 # preferred maximum degree per node

# Each cluster must connect to the rest of the network via at least this many
# independent links
CLUSTER_MIN_EXTERNAL_LINKS: int = 2

# Maximum attempts to generate a valid topology before giving up
TOPOLOGY_MAX_ATTEMPTS: int = 200

# ---------------------------------------------------------------------------
# 2) Link-delay model (Part 1, section 2-c)
# ---------------------------------------------------------------------------
# Base delay per unit of Euclidean distance = 1 millisecond
MS_PER_UNIT: float = 1.0

# Jitter = 20% of the base delay, drawn from U(-0.2*Base, +0.2*Base)
JITTER_FRACTION: float = 0.20

# ---------------------------------------------------------------------------
# 3) Evaluation-scenario parameters (Part 2)
# ---------------------------------------------------------------------------
N_PACKETS: int = 200                # number of packets per scenario
# Packet-generation time window (logical ms) -- no predetermined pattern
PACKET_INJECTION_WINDOW_MS: float = 5000.0

# Values of the Dandelion parameter p (parameter sweep)
P_VALUES = (0.9, 0.5, 0.1)

# Number of independent runs (different seeds) for mean/median/std reporting
N_SEEDS: int = 5

# Coverage threshold for the time metric (T80%)
COVERAGE_THRESHOLD: float = 0.80

# ---------------------------------------------------------------------------
# 4) Adversary model (Part 1, section 3)
# ---------------------------------------------------------------------------
# Maximum fraction of nodes the adversary is allowed to bribe
MAX_BRIBE_FRACTION: float = 0.30

# ---------------------------------------------------------------------------
# 5) Real UDP runtime (udp_runtime)
# ---------------------------------------------------------------------------
UDP_HOST: str = "127.0.0.1"         # everything runs on localhost
UDP_BASE_PORT: int = 21000          # first UDP port; each node gets its own port
UDP_CONTROLLER_PORT: int = 20999    # controller/simulator port for packet injection

# Time-scale factor: real sleep = logical delay (ms) * TIME_SCALE
# 1.0 means real time; smaller values make the UDP demo run faster.
# 0.2 is a good balance: fast, yet timing-faithful to the analysis engine.
UDP_TIME_SCALE: float = 0.2

# ---------------------------------------------------------------------------
# 6) Output paths
# ---------------------------------------------------------------------------
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(_THIS_DIR)               # CN-Project root
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")     # CSV files
FIGURES_DIR = os.path.join(PROJECT_DIR, "figures")     # plots
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")           # simulator reference log


def ensure_dirs() -> None:
    """Create output directories if they do not exist."""
    for d in (RESULTS_DIR, FIGURES_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)
