"""MABLE scheduling optimisation. """
"""
TO DO:
The cost_fn is Person 1's interface:
    cost_fn(vessel, schedule) -> TOTAL monetary cost of that schedule.
Without it this module is still a *feasibility* scheduler, NOT a cost
optimiser. Cost values are deliberately omitted in that case.
"""

from dataclasses import dataclass, field
from math import isfinite
from random import Random
from time import monotonic


@dataclass
class PlanningResult:
    schedules: dict = field(default_factory=dict)       # vessel -> new Schedule
    scheduled_trades: list = field(default_factory=list)  # inserted Trade objects
    costs: dict = field(default_factory=dict)            # trade -> sequential marginal cost
    total_extra_cost: float = 0.0


class SchedulingOptimizer:
    """Feasible-first, minimum-cost insertion with multi-start search.

    Pre-auction: maximises the number of trades assigned, then minimises
    the total incremental operating cost.
    Post-auction: same coverage priority, then maximises total *awarded*
    payments minus incremental cost (only relevant if some trades cannot
    be scheduled). Never deliberately drops an awarded contract merely
    because its marginal cost exceeds its payment.

    This is an AAMAS-inspired heuristic, not an exact implementation of
    the winning algorithm or proof that all awarded trades are feasible.
    """

    def __init__(self, fleet, cost_fn=None, restarts=8,
                 time_budget_s=8.0, seed=42):
        self.fleet = list(fleet)
        if cost_fn is not None and not callable(cost_fn):
            raise TypeError("cost_fn must be callable or None")
        self.cost_fn = cost_fn
        self.restarts = max(1, int(restarts))
        self.time_budget_s = max(0.1, float(time_budget_s))
        self.seed = seed

    def _cost(self, vessel, schedule):
        value = float(self.cost_fn(vessel, schedule))
        if not isfinite(value) or value < 0:
            raise ValueError("Schedule cost must be finite and non-negative")
        return value

    def _candidates(self, base_schedule, trade, append_only):
        """Yield independently copied, feasible insertion alternatives.

        MABLE's documented locations are indices from the *base* schedule.
        Its add_transportation() performs the pickup/dropoff offset itself.
        """
        if append_only:
            trial = base_schedule.copy()
            trial.add_transportation(trade)
            if trial.verify_schedule():
                yield trial
            return

        points = list(base_schedule.get_insertion_points())
        for pickup in points:
            for dropoff in points:
                if dropoff < pickup:
                    continue
                trial = base_schedule.copy()
                try:
                    trial.add_transportation(
                        trade,
                        location_pick_up=pickup,
                        location_drop_off=dropoff,
                    )
                except (ValueError, IndexError, AssertionError):
                    # An insertion point pair may not be accepted.
                    continue
                if trial.verify_schedule():
                    yield trial

    def best_insertion(self, trade, tentative_schedules, deadline=None,
                       append_only=False):
        """Return (vessel, copied_candidate_schedule, marginal_cost), or None.

        Never mutates vessel.schedule or any other live MABLE object.