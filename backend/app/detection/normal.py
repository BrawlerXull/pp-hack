"""Observed-vs-normal multipliers ("×6 vs normal") for the dashboard.

Always computed from our own baseline (Baseline.public(), as stored in incident.features["baseline"]), never
from the `baseline` an AI evidence item carries: that one is the model's text and may be invented.
Only features with a meaningful "normal" get a multiplier; shares like top_sku_share have none.
"""

from typing import Any

from app.detection.features import Baseline, Features

# A multiplier against a normal this small is noise (e.g. 3 errors against a 0.1% error rate = ×1000).
_MIN_NORMAL = {"decline_rate": 0.01, "error_rate": 0.005}
# Same for volume on a quiet shop: 3 orders an hour make 0.25 per 5 minutes, and two genuine orders would
# read "×8". Count at least 2 attempts per window as normal (a card-testing burst is still ×20 and more).
_MIN_ATTEMPTS_PER_WINDOW = 2.0


def normal_value(feature: str, window_min: int, baseline: dict[str, Any]) -> float | None:
    """What this feature usually is for the shop, over the same window."""
    per_hour = baseline.get("attempts_per_hour_mean")
    if feature == "attempts" and per_hour is not None:
        return max(float(per_hour) * window_min / 60, _MIN_ATTEMPTS_PER_WINDOW)
    if feature == "attempts_per_min" and per_hour is not None:
        return max(float(per_hour) / 60, _MIN_ATTEMPTS_PER_WINDOW / window_min)
    if feature in ("decline_rate", "error_rate") and baseline.get(feature) is not None:
        return max(float(baseline[feature]), _MIN_NORMAL[feature])
    return None


def vs_normal(
    feature: str, window_min: int, observed: float | None, baseline: dict[str, Any]
) -> float | None:
    """observed / normal, one decimal. None when there is no normal for this feature, or nothing observed."""
    normal = normal_value(feature, window_min, baseline)
    if observed is None or not normal:
        return None
    return round(observed / normal, 1)


def evidence_vs_normal(verdict: dict[str, Any] | None, features: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per verdict evidence item: our computed value for it, the shop's normal, and the multiplier.
    The observed number is read from our stored features (the item was validated against them)."""
    baseline = features.get("baseline") or {}
    out = []
    for ev in (verdict or {}).get("evidence", []):
        feature, window = ev.get("feature"), ev.get("window_min")
        observed = (features.get(f"{window}m") or {}).get(feature)
        ok = isinstance(window, int) and isinstance(observed, int | float)
        normal = normal_value(feature, window, baseline) if ok else None
        out.append(
            {
                "feature": feature,
                "window_min": window,
                "observed": observed,
                "normal": round(normal, 3) if normal is not None else None,
                "multiplier": vs_normal(feature, window, observed, baseline) if ok else None,
            }
        )
    return out


def live_snapshot(features: dict[int, Features], baseline: Baseline) -> dict[str, Any]:
    """What the War Room tiles show: the last 5 and 15 minutes as the detector saw them, with ×N vs normal."""
    base = baseline.public()
    out: dict[str, Any] = {"baseline": base}
    for window in (5, 15):
        f = features[window]
        out[f"{window}m"] = {
            "attempts": f.attempts,
            "attempts_vs_normal": vs_normal("attempts", window, f.attempts, base),
            "decline_rate": round(f.decline_rate, 3),
            "decline_rate_vs_normal": vs_normal("decline_rate", window, f.decline_rate, base)
            if f.reached_paypal
            else None,
            "captured": f.captured,
            "stopped": f.blocked_or_challenged + f.sca_required,
        }
    return out
