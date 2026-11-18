"""
The planner requires a monetary schedule-cost function from Person 1:
    cost_fn(vessel, schedule) -> float

"""

from dataclasses import dataclass
from math import isfinite
from random import Random
from time import monotonic


@dataclass
class PlanningResult:
    schedules: dict          # Vessel -> proposed updated Schedule
    scheduled_trades: list   # Trades successfully inserted
    costs: dict             # Trade -> incremental cost at time of insertion


class SchedulePlanner:
    """Greedy minimum-cost insertion with optional randomized restarts.

    Feasibility is checked by MABLE's Schedule.verify_schedule().
    Does not alter live vessel schedules; all insertions are on copies.

    Scoring: first maximise number of scheduled trades; then minimise
    additional total cost relative to starting vessel schedules.
    """

    def __init__(self, fleet, cost_fn, restarts=1, time_budget_s=12.0, seed=42):
        if not callable(cost_fn):
            raise TypeError("cost_fn must be callable: cost_fn(vessel, schedule)")
        self.fleet = list(fleet)
        self.cost_fn = cost_fn
        self.restarts = max(1, int(restarts))
        self.time_budget_s = max(0.1, float(time_budget_s))
        self.seed = seed

    def _cost(self, vessel, schedule):
        value = float(self.cost_fn(vessel, schedule))
        if not isfinite(value) or value < 0:
            raise ValueError("cost_fn must return a finite, nonnegative monetary cost")
        return value

    def best_insertion(self, trade, schedules, deadline=None):
        """Return (vessel, candidate_schedule, additional_cost), or None.

        `schedules` holds tentative plans built in this run; vessels absent
        from it still use their existing real `vessel.schedule` (a copy).
        """
        best = None
        for vessel in self.fleet:
            if deadline is not None and monotonic() >= deadline:
                return best

            base = schedules.get(vessel)
            if base is None:
                base = vessel.schedule
            base_cost = self._cost(vessel, base)
            positions = list(base.get_insertion_points())

            for pickup_point in positions:
                for dropoff_point in positions:
                    if dropoff_point < pickup_point:
                        continue  # Pickup must happen before drop-off.
                    if deadline is not None and monotonic() >= deadline:
                        return best

                    candidate = base.copy()
                    try:
                        candidate.add_transportation(
                            trade,
                            location_pick_up=pickup_point,
                            location_drop_off=dropoff_point,
                        )
                    except (ValueError, IndexError, AssertionError):
                        # Some position pairs may be disallowed by MABLE.
                        continue

                    if not candidate.verify_schedule():
                        continue

                    extra_cost = self._cost(vessel, candidate) - base_cost
                    if best is None or extra_cost < best[2]:
                        best = (vessel, candidate, extra_cost)

        return best

    def _one_run(self, ordered_trades, deadline):
        schedules = {}
        assigned = []
        incremental_costs = {}

        for trade in ordered_trades:
            if monotonic() >= deadline:
                break
            choice = self.best_insertion(trade, schedules, deadline)
            if choice is None:
                continue
            vessel, candidate, additional_cost = choice
            schedules[vessel] = candidate
            assigned.append(trade)
            incremental_costs[trade] = additional_cost

        return PlanningResult(schedules, assigned, incremental_costs)

    @staticmethod
    def _rank(result):
        # Rank by coverage first, then the sum of new operating costs.
        return (-len(result.scheduled_trades), sum(result.costs.values()))

    def plan(self, trades):
        """Construct a feasible low-cost tentative plan for the given trades.

        `restarts=1` = Lab 2's greedy baseline, but with cost-aware insertion.
        `restarts>1` = simplified AAMAS multi-start heuristic.
        """
        trades = list(trades)
        if not trades:
            return PlanningResult({}, [], {})

        rng = Random(self.seed)
        deadline = monotonic() + self.time_budget_s
        best = None