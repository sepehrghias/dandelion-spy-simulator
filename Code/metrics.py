"""
metrics.py
==========
Performance-evaluation metrics per Part 3 of the specification.

(a) Adversary performance score:
        Score_adv = Accuracy / Number_of_Bribed_Nodes
    where Accuracy is the fraction of correctly estimated sources over 200 packets.

(b) Honest-network performance score:
        Score_honest = (1 / T80%) * (1 - Adversary_Detection_Rate)
    where T80% is the time (seconds) to reach 80% of the nodes and
    Adversary_Detection_Rate equals the adversary's guessing accuracy.

Descriptive statistics (mean, median, std) for the parameter-sweep report are
also here.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import config
from sim_engine import ScenarioLog


# ---------------------------------------------------------------------------
# Descriptive statistics
# ---------------------------------------------------------------------------
@dataclass
class Stat:
    mean: float
    median: float
    std: float
    n: int

    def as_row(self) -> Dict[str, float]:
        return {"mean": self.mean, "median": self.median, "std": self.std, "n": self.n}


def describe(values: Sequence[float]) -> Stat:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return Stat(float("nan"), float("nan"), float("nan"), 0)
    mean = statistics.fmean(vals)
    median = statistics.median(vals)
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    return Stat(mean, median, std, len(vals))


# ---------------------------------------------------------------------------
# Network metrics (coverage time)
# ---------------------------------------------------------------------------
def t80_per_packet_ms(log: ScenarioLog,
                      threshold: float = config.COVERAGE_THRESHOLD) -> List[Optional[float]]:
    """Time for each packet to reach `threshold` of the nodes (ms); None if unreached."""
    return [pl.t_coverage(log.n_nodes, threshold) for pl in log.packets]


def t80_seconds(log: ScenarioLog,
                threshold: float = config.COVERAGE_THRESHOLD) -> Optional[float]:
    """Mean T80% in seconds (over packets that reached the threshold)."""
    vals = [t for t in t80_per_packet_ms(log, threshold) if t is not None]
    if not vals:
        return None
    return (sum(vals) / len(vals)) / 1000.0


def reach_rate(log: ScenarioLog,
               threshold: float = config.COVERAGE_THRESHOLD) -> float:
    """Fraction of packets that reached the coverage threshold."""
    vals = t80_per_packet_ms(log, threshold)
    return sum(1 for t in vals if t is not None) / len(vals)


def coverage_fraction(log: ScenarioLog) -> float:
    """Mean fraction of nodes each packet reached."""
    return log.coverage_fraction_mean()


# ---------------------------------------------------------------------------
# Adversary metrics
# ---------------------------------------------------------------------------
def accuracy(guesses: Dict[str, Optional[int]], log: ScenarioLog) -> float:
    """
    Fraction of correctly estimated sources. Exactly one guess per packet.
    A None guess (no info reached the adversary) counts as incorrect.
    """
    correct = 0
    for pl in log.packets:
        if guesses.get(pl.packet_id) == pl.source:
            correct += 1
    return correct / len(log.packets)


def score_adv(acc: float, n_bribed: int) -> float:
    """Score_adv = Accuracy / Number_of_Bribed_Nodes."""
    if n_bribed <= 0:
        return 0.0
    return acc / n_bribed


def score_honest(t80_sec: Optional[float], detection_rate: float) -> float:
    """Score_honest = (1/T80%) * (1 - Adversary_Detection_Rate)."""
    if t80_sec is None or t80_sec <= 0:
        return 0.0
    return (1.0 / t80_sec) * (1.0 - detection_rate)


# ---------------------------------------------------------------------------
# Full metric bundle for one scenario
# ---------------------------------------------------------------------------
@dataclass
class ScenarioMetrics:
    accuracy: float
    n_bribed: int
    score_adv: float
    t80_sec: Optional[float]
    reach_rate: float
    coverage: float
    score_honest: float
    n_spy_observed: float          # mean number of spies that saw each packet

    def as_row(self) -> Dict[str, float]:
        return {
            "accuracy": self.accuracy,
            "n_bribed": self.n_bribed,
            "score_adv": self.score_adv,
            "t80_sec": self.t80_sec if self.t80_sec is not None else float("nan"),
            "reach_rate": self.reach_rate,
            "coverage": self.coverage,
            "score_honest": self.score_honest,
            "n_spy_observed": self.n_spy_observed,
        }


def compute_scenario_metrics(log: ScenarioLog,
                             guesses: Dict[str, Optional[int]]) -> ScenarioMetrics:
    acc = accuracy(guesses, log)
    n_bribed = len(log.spy_set)
    t80 = t80_seconds(log)
    obs_counts = [len({o.spy for o in pl.spy_obs}) for pl in log.packets]
    return ScenarioMetrics(
        accuracy=acc,
        n_bribed=n_bribed,
        score_adv=score_adv(acc, n_bribed),
        t80_sec=t80,
        reach_rate=reach_rate(log),
        coverage=coverage_fraction(log),
        score_honest=score_honest(t80, acc),
        n_spy_observed=sum(obs_counts) / len(obs_counts) if obs_counts else 0.0,
    )
