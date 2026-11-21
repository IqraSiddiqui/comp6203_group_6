#person 3 add your code here
"""Person 3: standalone bidding strategy for the MABLE shipping agent.

This is an ADAPTATION of Person 3's Phase 1/2 strategy to Person 2's
SchedulingOptimizer. It does not import Person 3's alternative Company6,
_VesselModel, _Plan, or post-auction scheduling implementation.

Pre-auction: sample distinct feasible plans; retain the best K; estimate
appearance probability p_r and bid 0.5 * (dedicated_cost + shared_cost)
for requests with p_r >= P_SELECT. Fuel costs are interim estimates until
Person 1's full cost model is available.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import random
from time import monotonic

from loguru import logger
from mable.transport_operation import Bid

from source.scheduler import SchedulingOptimizer


# From Person 3's Phase 2. Smaller search budgets fit MABLE's inform timeout
# when using Person 2's verification-heavy MABLE scheduler.
P_SELECT = 0.60
PRE_RESTARTS = 12
PRE_TOP_K = 8
INFORM_BUDGET_S = 12.0
MIN_BID = 0.01
RANDOM_SEED = 6203


@dataclass
class _CandidatePlan:
    signature: tuple
    assignments: dict  # id(trade) -> vessel
    allocation: dict  # id(trade) -> allocated fuel cost
    coverage: int
    efficiency: float


class BiddingStrategy:
    """Compute bids independently of the company's receive() implementation.

    Expected `company` API: .fleet, .headquarters.get_network_distance(...),
    and optionally .estimate_schedule_cost(vessel, schedule) from Person 1.
    """

    def __init__(
        self, company, *, p_select=P_SELECT, pre_restarts=PRE_RESTARTS,
        top_k=PRE_TOP_K, time_budget_s=INFORM_BUDGET_S, seed=RANDOM_SEED,
    ):
        self.company = company
        self.p_select = float(p_select)
        self.pre_restarts = max(1, int(pre_restarts))
        self.top_k = max(1, int(top_k))
        self.time_budget_s = max(0.2, float(time_budget_s))
        self.seed = int(seed)
        self._distance_cache = {}

    def _distance(self, a, b):
        if a is None or b is None:
            raise ValueError("Unknown position when estimating transport cost")
        ka = getattr(a, "name", repr(a))
        kb = getattr(b, "name", repr(b))
        key = (ka, kb)
        if key not in self._distance_cache:
            if ka == kb:
                distance = 0.0
            else:
                distance = self.company.headquarters.get_network_distance(a, b)
            if distance is None:
                raise ValueError("Sea route unavailable")
            distance = float(distance)
            if not math.isfinite(distance) or distance < 0:
                raise ValueError("Invalid sea distance")
            self._distance_cache[key] = distance
        return self._distance_cache[key]

    @staticmethod
    def _position(vessel):
        """Prefer a journey's destination over trying to route from an at-sea point."""
        location = vessel.location
        return getattr(location, "destination", location)

    @staticmethod
    def _simple_schedule(schedule):
        return list(schedule.get_simple_schedule())

    @staticmethod
    def _transfer_cost(vessel, trade, pickup):
        hours = float(vessel.get_loading_time(trade.cargo_type, trade.amount))
        fn = vessel.get_loading_consumption if pickup else vessel.get_unloading_consumption
        return float(vessel.get_cost(fn(hours)))

    @staticmethod
    def _sailing_cost(vessel, distance, laden):
        speed = float(vessel.speed)
        if speed <= 0:
            raise ValueError("Vessel speed must be positive")
        hours = distance / speed
        fn = vessel.get_laden_consumption if laden else vessel.get_ballast_consumption
        return float(vessel.get_cost(fn(hours, vessel.speed)))

    def _direct_cost(self, vessel, trade):
        return (
            self._sailing_cost(
                vessel, self._distance(trade.origin_port, trade.destination_port), True
            )
            + self._transfer_cost(vessel, trade, True)
            + self._transfer_cost(vessel, trade, False)
        )

    def _dedicated_cost(self, vessel, trade):
        """Person 3's AbsoluteCost: after current commitments, do this trade alone."""
        base = self._simple_schedule(vessel.schedule)
        if base:
            kind, last_trade = base[-1]
            start = (
                last_trade.destination_port if kind == "DROP_OFF"
                else last_trade.origin_port
            )
        else:
            start = self._position(vessel)
        return (
            self._sailing_cost(vessel, self._distance(start, trade.origin_port), False)
            + self._direct_cost(vessel, trade)
        )

    def _route_allocation(self, vessel, schedule):
        """Adapt Person 3's allocate(): share sailing legs among onboard cargo.

        Include the incoming pickup in a sailing leg's beneficiaries.
        Loading and unloading are charged to the matching request. Idling is
        excluded, consistent with Person 3's interim fuel-cost model.

        Returns (total_fuel_cost, {id(trade): attributed_cost}, direct_sum).
        This is an estimate, NOT Person 1's eventual official cost model.
        """
        stops = self._simple_schedule(schedule)
        pickups = {id(trade) for kind, trade in stops if kind == "PICK_UP"}
        onboard = {
            id(trade): trade for kind, trade in stops
            if kind == "DROP_OFF" and id(trade) not in pickups
        }
        allocation = defaultdict(float)
        location = self._position(vessel)
        total = 0.0
        direct_sum = 0.0
        # If a scheduled transfer is already underway, its first sailing leg
        # must not be charged again. Mirrors Person 3's first_in_progress check.
        try:
            insertion_points = list(schedule.get_insertion_points())
            first_in_progress = bool(stops and insertion_points and insertion_points[0] > 1)
        except (AttributeError, TypeError):
            first_in_progress = False
        for index, (kind, trade) in enumerate(stops):
            pickup = kind == "PICK_UP"
            port = trade.origin_port if pickup else trade.destination_port
            distance = 0.0 if (index == 0 and first_in_progress) else self._distance(location, port)
            leg_cost = self._sailing_cost(vessel, distance, bool(onboard))
            total += leg_cost
            sharers = list(onboard.keys())
            if pickup and id(trade) not in sharers:
                sharers.append(id(trade))
            if sharers:
                part = leg_cost / len(sharers)
                for trade_id in sharers:
                    allocation[trade_id] += part
            handling = self._transfer_cost(vessel, trade, pickup)
            total += handling
            allocation[id(trade)] += handling
            if pickup:
                direct_sum += self._direct_cost(vessel, trade)
                onboard[id(trade)] = trade
            else:
                onboard.pop(id(trade), None)
            location = port
        if any(not math.isfinite(x) or x < 0 for x in [total, direct_sum, *allocation.values()]):
            raise ValueError("Non-finite or negative estimated fuel cost")
        return total, allocation, direct_sum

    def _candidate(self, result, offered):
        """Convert a Person 2 PlanningResult into a bid-usable plan."""
        scheduled_ids = {id(t) for t in result.scheduled_trades}
        offered_ids = {id(t) for t in offered}
        assignments = {}
        allocation = {}
        total_cost = 0.0
        direct_sum = 0.0
        signature = []
        for vessel in self.company.fleet:
            schedule = result.schedules.get(vessel, vessel.schedule)
            stops = self._simple_schedule(schedule)
            signature.append((id(vessel), tuple((kind, id(t)) for kind, t in stops)))
            route_total, route_alloc, route_direct = self._route_allocation(vessel, schedule)
            total_cost += route_total
            direct_sum += route_direct
            for kind, trade in stops:
                key = id(trade)
                if kind == "PICK_UP" and key in scheduled_ids and key in offered_ids:
                    assignments[key] = vessel
                    # If Person 1 supplied a schedule-cost function, Person 2
                    # records its sequential marginal cost for this insertion.
                    # Use that monetary estimate instead of the interim shared-
                    # fuel allocation. This is an adaptation (not identical to
                    # Person 3's shared-leg allocation definition).
                    person1_delta = result.costs.get(trade)
                    if person1_delta is not None and math.isfinite(float(person1_delta)):
                        allocation[key] = max(0.0, float(person1_delta))
                    else:
                        allocation[key] = route_alloc.get(key, 0.0)
        coverage = len(assignments)
        efficiency = direct_sum / total_cost if total_cost > 0 else 0.0
        return _CandidatePlan(tuple(signature), assignments, allocation, coverage, efficiency)

    def _top_plans(self, trades, deadline):
        rng = random.Random(self.seed)
        plans = {}
        cost_fn = getattr(self.company, "estimate_schedule_cost", None)
        if not callable(cost_fn):
            cost_fn = None  # Before Person 1 finishes, scheduler runs feasibility-first.
        for run in range(self.pre_restarts):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            order = trades.copy()
            if run:
                rng.shuffle(order)
            # A fresh scheduler can explore a different trade order. A single
            # restart per candidate prevents multiplying expensive searches.
            planner = SchedulingOptimizer(
                fleet=self.company.fleet,
                cost_fn=cost_fn,
                restarts=1,
                time_budget_s=max(0.1, min(remaining, 1.0)),
                seed=self.seed + run,
            )
            try:
                result = planner.plan(order)
                plan = self._candidate(result, trades)
            except Exception:
                logger.exception("Bidding: failed to evaluate candidate plan {}", run)
                continue
            old = plans.get(plan.signature)
            if old is None or (plan.coverage, plan.efficiency) > (old.coverage, old.efficiency):
                plans[plan.signature] = plan
        # Feasibility first; among equally feasible plans, Person 3's
        # consolidation efficiency criterion decides which are best.
        return sorted(plans.values(), key=lambda p: (p.coverage, p.efficiency), reverse=True)[:self.top_k]

    def make_bids(self, trades):
        trades = list(trades)
        if not trades:
            return []
        plans = self._top_plans(trades, monotonic() + self.time_budget_s)
        if not plans:
            logger.warning("Bidding: no evaluable feasible plans; no bids")
            return []
        frequencies = Counter()
        allocated_sums = defaultdict(float)
        vessel_votes = defaultdict(Counter)
        for plan in plans:
            for trade_id, vessel in plan.assignments.items():
                frequencies[trade_id] += 1
                allocated_sums[trade_id] += plan.allocation[trade_id]
                vessel_votes[trade_id][vessel] += 1
        bids = []
        for trade in trades:
            key = id(trade)
            if not frequencies[key]:
                continue
            appearance = frequencies[key] / len(plans)
            if appearance < self.p_select:
                continue
            vessel = vessel_votes[key].most_common(1)[0][0]
            try:
                absolute = self._dedicated_cost(vessel, trade)
                marginal = allocated_sums[key] / frequencies[key]
                amount = max(MIN_BID, 0.5 * (absolute + marginal))
                if not math.isfinite(amount):
                    raise ValueError("Non-finite bid")
            except Exception:
                logger.exception("Bidding: could not price trade {}; skipping", trade)
                continue
            bids.append(Bid(amount=amount, trade=trade))
        logger.info(
            "Bidding: {} bids for {} trades, based on {} distinct plans (p_select={:.2f})",
            len(bids), len(trades), len(plans), self.p_select,
        )
        return bids
