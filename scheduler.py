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
        """
        best = None
        for vessel in self.fleet:
            if deadline is not None and monotonic() >= deadline:
                break
            base = tentative_schedules.get(vessel)
            if base is None:
                base = vessel.schedule
            old_cost = self._cost(vessel, base) if self.cost_fn else 0.0
            for candidate in self._candidates(base, trade, append_only):
                extra_cost = (
                    self._cost(vessel, candidate) - old_cost
                    if self.cost_fn else 0.0
                )
                if best is None or extra_cost < best[2]:
                    best = (vessel, candidate, extra_cost)
                # A feasibility-only run needn't explore more alternatives
                # for this vessel; there is no meaningful cost comparison.
                if self.cost_fn is None:
                    break
                if deadline is not None and monotonic() >= deadline:
                    break
            if best is not None and self.cost_fn is None:
                break
        return best

    def _assign(self, result, trade, choice):
        vessel, candidate, delta = choice
        result.schedules[vessel] = candidate
        result.scheduled_trades.append(trade)
        result.total_extra_cost += delta
        if self.cost_fn is not None:
            result.costs[trade] = delta

    def _one_run(self, order, deadline, append_only=False):
        result = PlanningResult()
        for trade in order:
            if monotonic() >= deadline:
                break
            best = self.best_insertion(
                trade, result.schedules, deadline=deadline,
                append_only=append_only,
            )
            if best is not None:
                self._assign(result, trade, best)
        return result

    def _post_auction_run(self, order, payments, deadline):
        """Revenue-driven insertion on awarded contracts (AAMAS-inspired).

        At each step select a feasible trade/vessel/position combination
        with maximum payment - marginal cost. All feasible awarded trades
        remain eligible even when this value is negative, because failing
        to fulfil a won contract may incur a penalty.
        """
        result = PlanningResult()
        remaining = list(order)
        while remaining and monotonic() < deadline:
            best_choice = None
            best_trade = None
            best_gain = float("-inf")
            for trade in remaining:
                if monotonic() >= deadline:
                    break
                choice = self.best_insertion(trade, result.schedules, deadline)
                if choice is None:
                    continue
                gain = float(payments.get(trade, 0.0)) - choice[2]
                if best_choice is None or gain > best_gain:
                    best_choice, best_trade, best_gain = choice, trade, gain
            if best_choice is None:
                break
            self._assign(result, best_trade, best_choice)
            remaining.remove(best_trade)
        return result

    @staticmethod
    def _latest_pickup(trade):
        value = getattr(trade, "latest_pickup", None)
        return float("inf") if value is None else value

    def _quality(self, result, payments):
        """Higher is better. All awarded commitments have priority."""
        revenue = (
            sum(float(payments.get(t, 0.0)) for t in result.scheduled_trades)
            if payments is not None else 0.0
        )
        return (
            len(result.scheduled_trades),
            revenue - result.total_extra_cost,
        )

    def plan(self, trades, payments=None):
        """Construct a good feasible tentative plan.

        payments: optional mapping of *awarded* Trade -> Contract.payment.
        Only use this for post-auction planning, not speculative bids.
        """
        trades = list(trades)
        if not trades:
            return PlanningResult()

        deadline = monotonic() + self.time_budget_s
        rng = Random(self.seed)

        # A quick append-only baseline is useful as a safe fallback if an
        # expensive minimum-cost search hits its decision-time budget.
        best = self._one_run(trades, deadline, append_only=True)

        orders = [trades.copy()]
        if self.restarts >= 2:
            orders.append(sorted(trades, key=self._latest_pickup))
        if self.restarts >= 3 and payments is not None:
            orders.append(sorted(trades,
                                 key=lambda t: -float(payments.get(t, 0.0))))

        while len(orders) < self.restarts:
            shuffled = trades.copy()
            rng.shuffle(shuffled)
            orders.append(shuffled)

        for order in orders[:self.restarts]:
            if monotonic() >= deadline:
                break
            if payments is None:
                candidate = self._one_run(order, deadline, append_only=False)
            else:
                candidate = self._post_auction_run(order, payments, deadline)
            if self._quality(candidate, payments) > self._quality(best, payments):
                best = candidate

        # Repair: sometimes a second attempt can insert an unscheduled
        # trade into the best tentative plan without disrupting commitments.
        assigned = set(best.scheduled_trades)
        for trade in trades:
            if monotonic() >= deadline:
                break
            if trade in assigned:
                continue
            choice = self.best_insertion(trade, best.schedules, deadline)
            if choice is None:
                continue
            self._assign(best, trade, choice)
            assigned.add(trade)

        return best
