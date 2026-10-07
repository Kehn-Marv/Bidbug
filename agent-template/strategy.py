"""
VelesHack 2026 – CoGNETs Challenge 4
Bidbug Strategy v10 (Ultimate): tuned pacing + allocation optimizer.

Combines the best of v1–v3:
  v2's proven battery state-machine   → reliable energy pacing, no crashes
  v3's grid search                    → optimal compute/security allocation
  v1's aggressive energy spending     → higher per-round CES utility

Key improvements:
  • Energy caps raised 8–12 % above v2 to recover v1's utility advantage
  • Compute/security split optimised per round (25-point grid search)
  • Endgame awareness: spend battery more freely in the final rounds
  • Multiple energy levels searched within the cap for market adaptation
  • Forward-looking reserve that shrinks as the game progresses
"""

from __future__ import annotations

from math import fsum, isfinite, sqrt
from statistics import stdev
from typing import Any, Dict, List, Mapping, Tuple

RESOURCES = ("compute", "energy", "security")
EPS = 1e-9

# ---------------------------------------------------------------------------
# Arena battery physics (from the specification)
# ---------------------------------------------------------------------------
BATTERY_DRAIN = 0.30
IDLE_DRAIN    = 0.004
RECHARGE      = 0.22
CUTOFF        = 0.05

# ---------------------------------------------------------------------------
# Battery state thresholds
# ---------------------------------------------------------------------------
HEALTHY_BATTERY  = 0.65
WATCH_BATTERY    = 0.45
CONSERVE_BATTERY = 0.30
CRITICAL_BATTERY = 0.15

# ---------------------------------------------------------------------------
# Energy caps per state
#
# Tuned between v1 (flat ~0.075) and v2's conservative schedule.
# Each level is nudged upward to recover v1's per-round utility while
# keeping v2's pacing discipline.
# ---------------------------------------------------------------------------
ENERGY_CAPS = {
    "healthy":  0.082,
    "watch":    0.068,
    "conserve": 0.054,
    "critical": 0.042,
    "survival": 0.028,
}

# ---------------------------------------------------------------------------
# Pacing parameters
#
# v2 used 0.05 / 28.0 / 0.12.
# v4 allows slightly more rest tolerance + shorter horizon → each active
# round gets a bigger energy budget (the lesson from v1's high utility).
# ---------------------------------------------------------------------------
TARGET_IDLE_FRACTION = 0.065
TARGET_RESERVE       = 0.10
PACE_HORIZON         = 25.0

# ---------------------------------------------------------------------------
# Floor margins
# ---------------------------------------------------------------------------
FLOOR_MARGIN_BASE = 1.08
FLOOR_MARGIN_MAX  = 1.18

# ---------------------------------------------------------------------------
# Market estimation
# ---------------------------------------------------------------------------
HISTORY_WINDOW = 4
DECAY          = 0.75

# ---------------------------------------------------------------------------
# Optimizer grid
#
# 5 energy fractions × 25 compute/security splits = 125 evaluations/round.
# ---------------------------------------------------------------------------
ENERGY_FRACS = (0.50, 0.65, 0.78, 0.90, 1.00)
SPLIT_GRID   = 25


# ===================================================================
# Helpers
# ===================================================================

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _finite(value: Any, fallback: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    return value if isfinite(value) else fallback


def _weights(profile: Mapping[str, Any]) -> Dict[str, float]:
    raw = profile.get("weights") or {}
    vals = {k: max(0.0, _finite(raw.get(k, 1.0 / 3.0), 1.0 / 3.0))
            for k in RESOURCES}
    total = sum(vals.values())
    if total <= EPS:
        return {k: 1.0 / 3.0 for k in RESOURCES}
    return {k: vals[k] / total for k in RESOURCES}


def _battery(profile: Mapping[str, Any],
             history: List[Dict[str, Any]]) -> float:
    features = profile.get("features") or {}
    latest = features.get("battery")
    if latest is not None:
        try:
            value = float(latest)
            if isfinite(value):
                return _clamp(value, 0.0, 1.0)
        except (TypeError, ValueError):
            pass
    for result in reversed(history):
        try:
            value = float(result.get("battery"))
            if isfinite(value):
                return _clamp(value, 0.0, 1.0)
        except (TypeError, ValueError):
            continue
    return 1.0


def _mobility(profile: Mapping[str, Any]) -> float:
    features = profile.get("features") or {}
    try:
        return _clamp(float(features.get("mobility", 0.0)), 0.0, 1.0)
    except (TypeError, ValueError):
        return 0.0


# ===================================================================
# Market estimation
# ===================================================================

def _estimate_others(
    history: List[Dict[str, Any]],
    resource: str,
) -> float:
    """
    Recency-weighted estimate of the rest-of-swarm bid S_k.

    From the published clearing price:
        S_k = lambda_k * C_k - our_previous_bid_k
    """
    samples: List[float] = []
    for result in reversed(history[-HISTORY_WINDOW:]):
        try:
            prices     = result.get("prices") or {}
            capacities = result.get("capacities") or {}
            bid        = result.get("bid") or {}
            if resource not in prices or resource not in capacities:
                continue
            total  = float(prices[resource]) * float(capacities[resource])
            mine   = float(bid.get(resource, 0.0))
            others = max(EPS, total - mine)
            if isfinite(others):
                samples.append(others)
        except (TypeError, ValueError, KeyError):
            continue

    if not samples:
        return 1.0

    weights = [DECAY ** i for i in range(len(samples))]
    estimate = fsum(v * w for v, w in zip(samples, weights)) / fsum(weights)
    return max(EPS, estimate)


def _floor_margin(
    history: List[Dict[str, Any]],
    resource: str,
) -> float:
    """Wider margin when the market estimate is noisy."""
    samples: List[float] = []
    for result in history[-HISTORY_WINDOW:]:
        try:
            p = float(result["prices"][resource])
            c = float(result["capacities"][resource])
            b = float((result.get("bid") or {}).get(resource, 0.0))
            s = max(EPS, p * c - b)
            if isfinite(s):
                samples.append(s)
        except (KeyError, TypeError, ValueError):
            continue

    if len(samples) < 2:
        return FLOOR_MARGIN_BASE

    mean = sum(samples) / len(samples)
    if mean <= EPS:
        return FLOOR_MARGIN_BASE

    vol = stdev(samples) / mean
    extra = _clamp(0.25 * vol, 0.0, FLOOR_MARGIN_MAX - FLOOR_MARGIN_BASE)
    return FLOOR_MARGIN_BASE + extra


# ===================================================================
# Battery state machine & energy pacing
# ===================================================================

def _battery_state(battery: float) -> str:
    if battery >= HEALTHY_BATTERY:
        return "healthy"
    if battery >= WATCH_BATTERY:
        return "watch"
    if battery >= CONSERVE_BATTERY:
        return "conserve"
    if battery >= CRITICAL_BATTERY:
        return "critical"
    return "survival"


def _rounds_elapsed(history: List[Dict[str, Any]]) -> int:
    """Estimate total rounds elapsed (including idle) from history."""
    for result in reversed(history):
        try:
            rnd = int(result.get("round", 0))
            if rnd > 0:
                return rnd
        except (TypeError, ValueError):
            continue
    return len(history)


def _energy_share_cap(
    battery: float,
    mobility: float,
    energy_weight: float,
    prices: Mapping[str, float],
    capacities: Mapping[str, float],
    elapsed: int,
) -> float:
    """
    Maximum energy share for this round.

    Combines v2's sustainable-drain pacing with tuned state caps and
    endgame awareness that progressively reduces the battery reserve.
    """
    state = _battery_state(battery)
    state_cap = ENERGY_CAPS[state]

    # Sustainable drain at target idle duty cycle
    sustainable = (
        TARGET_IDLE_FRACTION * RECHARGE
        / max(EPS, 1.0 - TARGET_IDLE_FRACTION)
    )

    # Endgame: shrink the reserve so more battery becomes spendable surplus.
    # In the last ~10 rounds, there is no point hoarding charge.
    if elapsed >= 52:
        reserve = CUTOFF + 0.005
    elif elapsed >= 45:
        reserve = 0.07
    elif elapsed >= 38:
        reserve = 0.085
    else:
        reserve = TARGET_RESERVE

    surplus = max(0.0, battery - reserve)

    # Shorten the pacing horizon in the endgame to spend faster.
    if elapsed >= 50:
        horizon = 9.0
    elif elapsed >= 42:
        horizon = 16.0
    else:
        horizon = PACE_HORIZON

    paced_drain = sustainable + surplus / horizon

    denominator = BATTERY_DRAIN * (1.0 + max(0.0, mobility))
    target = (paced_drain - IDLE_DRAIN) / max(EPS, denominator)

    # Cheap-energy bonus: when energy is unusually cheap relative to
    # compute/security, the CES utility gain justifies a small premium.
    try:
        pe = max(0.05, float(prices.get("energy", 1.0)))
        pc = max(0.05, float(prices.get("compute", 1.0)))
        ps = max(0.05, float(prices.get("security", 1.0)))
        ce = max(0.05, float(capacities.get("energy", 1.0)))
        cc = max(0.05, float(capacities.get("compute", 1.0)))
        cs = max(0.05, float(capacities.get("security", 1.0)))
        e_score = (energy_weight ** 2) * ce / pe
        o_score = ((1.0 - energy_weight) ** 2) * 0.5 * (cc / pc + cs / ps)
        if battery >= WATCH_BATTERY and e_score > 1.2 * max(EPS, o_score):
            target += 0.008
    except (TypeError, ValueError):
        pass

    return _clamp(target, 0.018, min(state_cap, 0.095))


# ===================================================================
# Kelly and utility
# ===================================================================

def _bid_for_share(others: float, share: float, capacity: float) -> float:
    """Invert Kelly: b = S * t / (C - t)."""
    if share <= 0.0 or capacity <= share + EPS:
        return 0.0
    return max(0.0, others * share / (capacity - share))


def _allocation(
    bid: Mapping[str, float],
    capacities: Mapping[str, float],
    others: Mapping[str, float],
) -> Dict[str, float]:
    """Predicted allocation from a candidate bid."""
    out: Dict[str, float] = {}
    for k in RESOURCES:
        c = max(EPS, _finite(capacities.get(k), 1.0))
        s = max(EPS, _finite(others.get(k), 1.0))
        b = max(0.0, _finite(bid.get(k), 0.0))
        out[k] = c * b / (s + b) if (s + b) > EPS else 0.0
    return out


def _utility(alloc: Mapping[str, float],
             weights: Mapping[str, float]) -> float:
    """CES utility: (Σ w_k √x_k)²."""
    total = 0.0
    for k in RESOURCES:
        x = max(0.0, _finite(alloc.get(k), 0.0))
        w = max(0.0, _finite(weights.get(k), 0.0))
        total += w * sqrt(x)
    return total * total


# ===================================================================
# Allocation optimizer
# ===================================================================

def _optimize_allocation(
    budget: float,
    energy_cap_share: float,
    capacities: Mapping[str, float],
    others: Mapping[str, float],
    weights: Mapping[str, float],
    q_min: float,
    s_min: float,
    margin_c: float,
    margin_s: float,
) -> Dict[str, float]:
    """
    Grid search over energy levels × compute/security splits.

    Energy is searched at a few fractions of the cap (not above it),
    allowing the optimizer to spend less energy when market conditions
    favour heavier compute/security investment — without the aggressive
    battery penalty that hurt v3.
    """
    e_cap = max(EPS, _finite(capacities.get("energy"), 1.0))
    c_cap = max(EPS, _finite(capacities.get("compute"), 1.0))
    s_cap = max(EPS, _finite(capacities.get("security"), 1.0))

    target_c = min(0.95 * c_cap, q_min * margin_c) if q_min > 0 else 0.0
    target_s = min(0.95 * s_cap, s_min * margin_s) if s_min > 0 else 0.0

    best_bid: Dict[str, float] = {}
    best_util = float("-inf")

    for e_frac in ENERGY_FRACS:
        target_xe = energy_cap_share * e_frac
        e_bid = _bid_for_share(others["energy"], target_xe, e_cap)
        if e_bid >= budget:
            continue

        remaining = budget - e_bid

        for i in range(SPLIT_GRID):
            c_frac = i / float(SPLIT_GRID - 1)
            b_c = remaining * c_frac
            b_s = remaining - b_c

            bid = {"compute": b_c, "energy": e_bid, "security": b_s}
            alloc = _allocation(bid, capacities, others)

            # Reject candidates that fail QoS floors
            if target_c > 0 and alloc["compute"] + 1e-7 < target_c:
                continue
            if target_s > 0 and alloc["security"] + 1e-7 < target_s:
                continue

            u = _utility(alloc, weights)
            if u > best_util:
                best_util = u
                best_bid = bid

    if best_bid:
        return best_bid
    return {}


def _fallback(
    budget: float,
    prices: Mapping[str, float],
    capacities: Mapping[str, float],
    weights: Mapping[str, float],
) -> Dict[str, float]:
    """Market-aware fallback (proven safe from v1/v2)."""
    scores: Dict[str, float] = {}
    for k in RESOURCES:
        price    = max(0.05, _finite(prices.get(k), 1.0))
        capacity = max(0.05, _finite(capacities.get(k), 1.0))
        scores[k] = (weights[k] ** 2) * capacity / price
    total = sum(scores.values())
    if total <= EPS:
        return {k: budget / 3.0 for k in RESOURCES}
    return {k: budget * scores[k] / total for k in RESOURCES}


# ===================================================================
# Floor enforcement
# ===================================================================

def _move_from_donors(
    bid: Dict[str, float],
    donor_keys: Tuple[str, ...],
    amount: float,
) -> float:
    """Remove up to `amount` of budget from donors, proportionally."""
    if amount <= 0.0:
        return 0.0
    available = sum(max(0.0, bid.get(k, 0.0)) for k in donor_keys)
    if available <= EPS:
        return 0.0
    take = min(amount, available)
    for k in donor_keys:
        current = max(0.0, bid.get(k, 0.0))
        share = current / available if available > EPS else 0.0
        bid[k] = max(0.0, current - take * share)
    return take


def _enforce_floor(
    bid: Dict[str, float],
    budget: float,
    resource: str,
    floor: float,
    capacity: float,
    others: float,
    margin: float,
) -> None:
    """Ensure a floor-bearing resource gets enough bid to clear its QoS target."""
    if floor <= 0.0 or capacity <= floor + EPS:
        return
    target   = min(floor * margin, 0.95 * capacity)
    required = _bid_for_share(others, target, capacity)
    if bid.get(resource, 0.0) + EPS >= required:
        return

    deficit = required - bid.get(resource, 0.0)
    spare   = max(0.0, budget - sum(bid.values()))
    if spare > EPS:
        use = min(spare, deficit)
        bid[resource] = bid.get(resource, 0.0) + use
        deficit -= use
    if deficit <= EPS:
        return

    donors = tuple(k for k in RESOURCES if k != resource)
    moved  = _move_from_donors(bid, donors, deficit)
    bid[resource] = bid.get(resource, 0.0) + moved


# ===================================================================
# Normalisation
# ===================================================================

def _normalise(bid: Mapping[str, float], budget: float) -> Dict[str, float]:
    clean: Dict[str, float] = {}
    for k in RESOURCES:
        v = _finite(bid.get(k), 0.0)
        clean[k] = max(0.0, v)
    total = sum(clean.values())
    if total <= EPS:
        return {k: budget / 3.0 for k in RESOURCES}
    if total > budget:
        factor = budget / total
        clean = {k: v * factor for k, v in clean.items()}
    return clean


# ===================================================================
# Main entry point
# ===================================================================

def decide_bid(
    budget: float,
    prices: Dict[str, float],
    capacities: Dict[str, float],
    profile: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> Dict[str, float]:
    """
    v10 strategy:
      1. Determine energy ceiling from the tuned pacing controller.
      2. Grid-search energy levels × compute/security splits.
      3. Enforce buffered QoS floors from inferred competition.
      4. Spend the whole budget; unused budget has no value.
    """
    budget = max(0.0, float(budget))
    if budget <= EPS:
        return {k: 0.0 for k in RESOURCES}

    w        = _weights(profile)
    battery  = _battery(profile, history)
    mobility = _mobility(profile)
    elapsed  = _rounds_elapsed(history)

    # ---- Energy ceiling from pacing controller ----
    energy_cap_share = _energy_share_cap(
        battery, mobility, w["energy"], prices, capacities, elapsed,
    )

    # ---- Market estimates ----
    others = {k: _estimate_others(history, k) for k in RESOURCES}

    # ---- Floor margins ----
    margin_c = _floor_margin(history, "compute")
    margin_s = _floor_margin(history, "security")
    q_min    = max(0.0, _finite(profile.get("q_min"), 0.0))
    s_min    = max(0.0, _finite(profile.get("s_min"), 0.0))

    # ---- Optimize allocation ----
    bid = _optimize_allocation(
        budget, energy_cap_share, capacities, others, w,
        q_min, s_min, margin_c, margin_s,
    )

    if not bid:
        bid = _fallback(budget, prices, capacities, w)

    # ---- Final floor enforcement ----
    _enforce_floor(
        bid, budget, "compute", q_min,
        max(EPS, float(capacities.get("compute", 1.0))),
        others["compute"], margin_c,
    )
    _enforce_floor(
        bid, budget, "security", s_min,
        max(EPS, float(capacities.get("security", 1.0))),
        others["security"], margin_s,
    )

    # ---- Spend remaining budget on compute/security ----
    total = sum(bid.values())
    if total < budget - EPS:
        remaining = budget - total
        cs = bid["compute"] + bid["security"]
        if cs <= EPS:
            bid["compute"]  += remaining * 0.5
            bid["security"] += remaining * 0.5
        else:
            bid["compute"]  += remaining * bid["compute"] / cs
            bid["security"] += remaining * bid["security"] / cs

    return _normalise(bid, budget)
