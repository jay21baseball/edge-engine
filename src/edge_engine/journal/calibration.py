"""Self-calibration: does the system's claimed edge actually show up?

This is the feature nobody builds, and it is the one that decides whether any of
the rest is real. A model that says 70% and resolves at 52% is not a slightly
worse model - it is a losing one, and the only way to find out cheaply is on
paper, before scaling.

Scaling rule enforced here: do not increase deployed bankroll until at least
MIN_SIGNALS_TO_SCALE resolved predictions show calibration within tolerance.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

MIN_SIGNALS_TO_SCALE = 100
MAX_ACCEPTABLE_BRIER = 0.25
MAX_CALIBRATION_ERROR = 0.10


@dataclass
class Bucket:
    low: float
    high: float
    n: int = 0
    predicted_sum: float = 0.0
    realized_sum: float = 0.0

    @property
    def predicted_mean(self) -> float:
        return self.predicted_sum / self.n if self.n else 0.0

    @property
    def realized_rate(self) -> float:
        return self.realized_sum / self.n if self.n else 0.0

    @property
    def error(self) -> float:
        return self.realized_rate - self.predicted_mean


@dataclass
class CalibrationReport:
    n: int = 0
    brier: float = 1.0
    mean_abs_error: float = 1.0
    buckets: list[Bucket] = field(default_factory=list)

    @property
    def is_calibrated(self) -> bool:
        return (self.n >= MIN_SIGNALS_TO_SCALE
                and self.brier <= MAX_ACCEPTABLE_BRIER
                and self.mean_abs_error <= MAX_CALIBRATION_ERROR)

    @property
    def cleared_to_scale(self) -> bool:
        return self.is_calibrated

    def verdict(self) -> str:
        if self.n < MIN_SIGNALS_TO_SCALE:
            return (f"INSUFFICIENT DATA - {self.n}/{MIN_SIGNALS_TO_SCALE} resolved "
                    f"signals. Do not increase bankroll yet.")
        if not self.is_calibrated:
            return (f"NOT CALIBRATED - Brier {self.brier:.3f} "
                    f"(max {MAX_ACCEPTABLE_BRIER}), mean abs error "
                    f"{self.mean_abs_error:.3f} (max {MAX_CALIBRATION_ERROR}). "
                    f"The edge estimates are not trustworthy. Do not scale.")
        return (f"CALIBRATED over {self.n} resolved signals - Brier "
                f"{self.brier:.3f}, mean abs error {self.mean_abs_error:.3f}. "
                f"Cleared to increase deployed bankroll.")

    def reliability_table(self) -> str:
        lines = [f"{'bucket':>12} {'n':>5} {'predicted':>10} {'realized':>9} "
                 f"{'error':>7}"]
        for b in self.buckets:
            if b.n == 0:
                continue
            lines.append(
                f"{f'{b.low:.0%}-{b.high:.0%}':>12} {b.n:>5} "
                f"{b.predicted_mean:>10.1%} {b.realized_rate:>9.1%} "
                f"{b.error:>+7.1%}"
            )
        return "\n".join(lines)


def build_report(predictions: list[tuple[float, float]],
                 n_buckets: int = 10) -> CalibrationReport:
    """`predictions` is a list of (predicted_probability, outcome in {0,1})."""
    report = CalibrationReport()
    edges = [i / n_buckets for i in range(n_buckets + 1)]
    report.buckets = [Bucket(edges[i], edges[i + 1]) for i in range(n_buckets)]

    clean = [(p, o) for p, o in predictions
             if p is not None and o is not None and 0.0 <= p <= 1.0]
    report.n = len(clean)
    if not clean:
        return report

    for prob, outcome in clean:
        idx = min(int(prob * n_buckets), n_buckets - 1)
        bucket = report.buckets[idx]
        bucket.n += 1
        bucket.predicted_sum += prob
        bucket.realized_sum += outcome

    report.brier = statistics.fmean([(p - o) ** 2 for p, o in clean])
    populated = [b for b in report.buckets if b.n > 0]
    report.mean_abs_error = (
        sum(abs(b.error) * b.n for b in populated) / report.n if populated else 1.0
    )
    return report
