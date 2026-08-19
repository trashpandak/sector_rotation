"""Weighting Engine -- same strategy-pattern design and same capped-weight
redistribution algorithm as the sibling Market Intelligence Engine project
(already tested there, including the infeasible-cap edge case)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List


class WeightingMethod(ABC):
    @abstractmethod
    def compute_weights(self, instrument_ids: List[int], market_caps: Dict[int, float], params: dict) -> Dict[int, float]:
        ...


class EqualWeight(WeightingMethod):
    def compute_weights(self, instrument_ids, market_caps, params) -> Dict[int, float]:
        if not instrument_ids:
            return {}
        w = 1.0 / len(instrument_ids)
        return {iid: w for iid in instrument_ids}


class MarketCapWeight(WeightingMethod):
    def compute_weights(self, instrument_ids, market_caps, params) -> Dict[int, float]:
        caps = {iid: max(market_caps.get(iid, 0.0), 0.0) for iid in instrument_ids}
        total = sum(caps.values())
        if total <= 0:
            return EqualWeight().compute_weights(instrument_ids, market_caps, params)
        weights = {iid: c / total for iid, c in caps.items()}
        cap_pct = params.get("cap_single_constituent_pct")
        if cap_pct:
            weights = _apply_weight_cap(weights, cap_pct / 100.0)
        return weights


def _apply_weight_cap(weights: Dict[int, float], max_weight: float) -> Dict[int, float]:
    """Iteratively cap any constituent above max_weight and redistribute the
    excess proportionally among uncapped constituents.

    Feasibility check uses the count of constituents with NONZERO weight, not
    the raw constituent count. This is a real bug fix: a constituent with
    zero weight (e.g. its market cap couldn't be computed because its price
    fetch failed that day -- see data_acquisition/sources.py) can never
    receive any of a capped constituent's redistributed excess (redistribution
    is proportional to current weight, and proportional-to-zero is zero). If
    the feasibility check only counted raw constituent numbers, a 4-constituent
    index where one constituent's data happened to be missing that day would
    be treated as "feasible" (4 * 30% = 120% >= 100%) when the EFFECTIVE
    number of constituents able to absorb weight was really 3 (3 * 30% = 90%
    < 100%, genuinely infeasible) -- the redistribution loop would then hit
    under_total <= 0 partway through and silently break, leaving the missing
    constituent at 0% and the others capped, weights summing to 90% instead
    of 100%. This was caught via a real production pipeline run's weight-sum
    validation errors, not synthetic testing.

    Design choice (differs from the sibling Market Intelligence Engine
    project's version of this function): when infeasible, weights fall back
    to UNCAPPED proportional market-cap weights (which sum to 1.0) rather
    than leaving the shortfall unallocated -- a cap meant to prevent one
    stock dominating a LARGE pool isn't really applicable when there are only
    1-2 constituents actually able to receive weight anyway.
    """
    weights = dict(weights)
    nonzero_count = sum(1 for w in weights.values() if w > 0)
    if nonzero_count == 0 or max_weight * nonzero_count < 1.0:
        return weights
    for _ in range(10):
        over = {iid: w for iid, w in weights.items() if w > max_weight}
        if not over:
            break
        excess = sum(w - max_weight for w in over.values())
        for iid in over:
            weights[iid] = max_weight
        under = {iid: w for iid, w in weights.items() if w < max_weight and w > 0}
        under_total = sum(under.values())
        if under_total <= 0:
            # No remaining constituent has nonzero weight to redistribute
            # the excess into (the only candidates left are either already
            # capped or permanently at zero). Give the excess back to the
            # currently-capped constituents proportionally rather than
            # dropping it -- mathematically the only place left for it to go,
            # and guarantees weights still sum to 1.0.
            over_total = sum(weights[iid] for iid in over)
            for iid in over:
                share = (weights[iid] / over_total) if over_total > 0 else (1.0 / len(over))
                weights[iid] += excess * share
            break
        for iid in under:
            weights[iid] += excess * (weights[iid] / under_total)
    return weights


REGISTRY = {"EQUAL": EqualWeight, "MCAP": MarketCapWeight}


def get_weighting_method(code: str) -> WeightingMethod:
    if code not in REGISTRY:
        raise ValueError(f"Unknown weighting method '{code}'")
    return REGISTRY[code]()
