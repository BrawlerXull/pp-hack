"""Small exact tail probabilities for the statistical triggers, in log space (no scipy: one Poisson sum, one
binomial sum and a CUSUM are not worth a 40 MB dependency).

Every test reports a "surprise" = -log10(p): 3 means "1 in 1,000 under the shop's normal", and it is a plain
number the evidence check and the number guard can compare (a p-value of 1e-9 cannot be cited in prose).
"""

import math
from collections.abc import Iterable

SURPRISE_CAP = 50.0  # p < 1e-50 is "never" either way; keeps the stored number readable


def _logsumexp(terms: list[float]) -> float:
    if not terms:
        return -math.inf
    top = max(terms)
    return top + math.log(sum(math.exp(t - top) for t in terms))


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    # 1 - P(X <= k-1) loses all precision in the far tail, so sum the upper tail directly until it fades.
    terms = []
    i = k
    while True:
        t = i * math.log(lam) - lam - math.lgamma(i + 1)
        terms.append(t)
        if i > lam and t < terms[0] - 40:
            break
        i += 1
    return min(1.0, math.exp(_logsumexp(terms)))


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    log_c = math.lgamma(n + 1)
    terms = [
        log_c - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * math.log(p) + (n - i) * math.log1p(-p)
        for i in range(k, n + 1)
    ]
    return min(1.0, math.exp(_logsumexp(terms)))


def surprise(p: float) -> float:
    """-log10(p), capped. 0 for p = 1."""
    if p <= 0:
        return SURPRISE_CAP
    return min(SURPRISE_CAP, max(0.0, -math.log10(p)))


def cusum(xs: Iterable[float], k: float) -> float:
    """One-sided CUSUM statistic at the end of the series: S_t = max(0, S_{t-1} + x_t - k). It grows while
    the per-step rate stays above the reference k and decays by k per quiet step, so it measures how long
    and how persistently something kept happening, not how big one burst was."""
    s = 0.0
    for x in xs:
        s = max(0.0, s + x - k)
    return s
