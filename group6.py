"""Group 6 maritime shipping agent for the COMP6203 MABLE competition.

The Company6 agent combines vessel cost estimation, route scheduling, auction
bidding, future-trade evaluation, and opponent-price diagnostics.

Algorithmic reference
---------------------
Mai, W., Ou, X., Liao, X., and Zhang, H. (2025), "Fast Scheduling and Robust
Pricing: An Automated Agent Framework for Competitive Shipping Company
Operations under Sealed-Bid Second-Price Auctions", AAMAS 2025.
https://research.tudelft.nl/files/246062082/group7_report.pdf

Approaches adapted from this paper:
- Feasible pickup/drop-off insertion and multi-start schedule construction
  (Section 3.1, Algorithms 1-3).
- Consolidation-efficiency ranking and top-K candidate routes (Section 3.1).
- Trade-appearance threshold, proportional route-cost allocation, and the
  equally weighted absolute/allocated-cost bid (Section 3.2, Algorithm 4).
- Payment-minus-additional-cost reasoning after auction awards (Section 3.3).

The implementation also includes its own extensions: vessel-assignment
sampling, ballast-aware and quality-weighted plan ranking, a common-plan
bid-set safeguard, penalty-aware scheduling, bounded local search, future
opportunity discounts, and diagnostic opponent-price calibration. These
extensions are not algorithms claimed by the cited paper.
"""


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------
# Standalone trade costs are used for bid valuation. Full schedule costs are
# used to compare routes; their difference gives the marginal insertion cost.
# All estimates use the simulation's vessel consumption and fuel price APIs.

import math

__all__ = [
    "estimate_trade_cost",
    "estimate_schedule_cost",
    "estimate_marginal_cost",
    "CostEstimationError",
]


class CostEstimationError(Exception):
    """Raised when a cost cannot be estimated using the available MABLE data."""
    pass


# 1. BASIC VALIDATION

def _check_number(value, description):
    """Check that a value is a valid finite number."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CostEstimationError(
            f"{description} is not a valid number: {value!r}"
        )

    if not math.isfinite(number) or number < 0:
        raise CostEstimationError(
            f"{description} must be finite and non-negative: {number}"
        )

    return number


# 2. SIMULATION AND NETWORK INFORMATION

def _get_engine(obj):
  

    if obj is None:
        return None

    engine = getattr(obj, "_engine", None)
    if engine is None:
        engine = getattr(obj, "engine", None)
    return engine


def _get_simulation_context(vessel, schedule=None):


    engine = None

    if schedule is not None:
        engine = _get_engine(schedule)

    if engine is None:
        engine = _get_engine(vessel)

    if engine is None:
        return None, None, None

    world = getattr(engine, "world", None)

    if world is None:
        return engine, None, None

    network = getattr(world, "network", None)

    return engine, world, network


def _get_distance(origin, destination, vessel, headquarters=None,
                  schedule=None):
    """Prefer the simulator network; use the company headquarters when needed.

    Headquarters.get_network_distance() is available to TradingCompany agents
    even when vessel/schedule objects do not expose a private engine.
    """
    _, _, network = _get_simulation_context(vessel, schedule)

    if network is not None:
        get_distance = getattr(network, "get_distance", None)
        if callable(get_distance):
            try:
                return _check_number(get_distance(origin, destination),
                                     "Travel distance")
            except (AttributeError, TypeError, ValueError, CostEstimationError):
                # Some MABLE versions expose a different network API.
                # Headquarters is a supported fallback in the agent.
                if headquarters is None:
                    raise

    if headquarters is not None:
        get_distance = getattr(headquarters, "get_network_distance", None)
        if callable(get_distance):
            try:
                return _check_number(get_distance(origin, destination),
                                     "Travel distance")
            except Exception as exc:
                raise CostEstimationError(
                    f"Headquarters could not find a sea distance: {exc}"
                ) from exc

    raise CostEstimationError(
        "No MABLE network distance method or headquarters is available."
    )


def _get_current_location(vessel, world, network):
    """Get current position, or a journey destination when at sea.

    An at-sea fallback is approximate: scheduling starts from the journey's
    destination after arrival rather than assuming teleportation mid-voyage.
    """
    location = getattr(vessel, "location", None)
    if location is not None and location.__class__.__name__ == "OnJourney":
        destination = getattr(location, "destination", None)
        if destination is None:
            raise CostEstimationError("The vessel is at sea with no destination.")
        return destination

    get_location = (
        getattr(network, "get_vessel_location", None)
        if network is not None else None
    )
    if callable(get_location) and world is not None:
        try:
            loc = get_location(vessel, world.current_time)
            if loc is not None:
                return loc
        except (TypeError, AttributeError):
            pass

    location = getattr(vessel, "location", None)
    if location is None:
        raise CostEstimationError("The vessel's current location is unavailable.")
    if location.__class__.__name__ == "OnJourney":
        destination = getattr(location, "destination", None)
        if destination is None:
            raise CostEstimationError(
                "The vessel is at sea, but no destination is available."
            )
        return destination
    return location


# 3. FUEL TO MONEY CONVERSION

def _fuel_cost(vessel, fuel_amount):
    """
    Convert fuel consumption into monetary cost.

    MABLE's fuel-consuming vessel provides get_cost().
    If that method is unavailable, the Lab 2 assumption is used:
    one unit of fuel equals one cost unit.
    """

    fuel_amount = _check_number(
        fuel_amount,
        "Fuel consumption"
    )

    get_cost = getattr(vessel, "get_cost", None)

    if callable(get_cost):
        try:
            cost = get_cost(fuel_amount)
        except CostEstimationError:
            raise
        except Exception as exc:
            raise CostEstimationError(
                f"vessel.get_cost() failed while costing "
                f"{fuel_amount} units of fuel: {exc}"
            ) from exc
        return _check_number(cost, "Fuel cost")

    return fuel_amount


# 4. TRAVEL COST

def _travel_cost(
    vessel,
    origin,
    destination,
    laden,
    headquarters=None,
    schedule=None
):
    """
    Calculate the cost and duration of a journey.

    If the vessel carries cargo:
        get_laden_consumption()

    If the vessel carries no cargo:
        get_ballast_consumption()
    """

    distance = _get_distance(
        origin,
        destination,
        vessel,
        headquarters,
        schedule
    )

    travel_time = vessel.get_travel_time(distance)

    travel_time = _check_number(
        travel_time,
        "Travel time"
    )

    if travel_time == 0:
        return 0.0, 0.0

    speed = getattr(vessel, "speed", None)

    if speed is None:
        raise CostEstimationError(
            "The vessel does not provide a speed value."
        )

    speed = _check_number(speed, "Vessel speed")

    if speed == 0:
        raise CostEstimationError(
            "The vessel's speed is zero; cannot compute fuel consumption."
        )

    # MABLE uses different fuel-consumption curves for loaded and empty legs.
    if laden:
        fuel = vessel.get_laden_consumption(
            travel_time,
            speed
        )
    else:
        fuel = vessel.get_ballast_consumption(
            travel_time,
            speed
        )

    cost = _fuel_cost(vessel, fuel)

    return cost, travel_time


# 5. LOADING AND UNLOADING COST

def _get_transfer_time(vessel, trade):
    """Calculate the time required to transfer the cargo."""

    duration = vessel.get_loading_time(
        trade.cargo_type,
        trade.amount
    )

    return _check_number(
        duration,
        "Loading or unloading time"
    )


def _loading_cost(vessel, trade):
    """Calculate fuel cost for loading cargo."""

    duration = _get_transfer_time(vessel, trade)

    fuel = vessel.get_loading_consumption(duration)

    return _fuel_cost(vessel, fuel), duration


def _unloading_cost(vessel, trade):
    """Calculate fuel cost for unloading cargo."""

    duration = _get_transfer_time(vessel, trade)

    fuel = vessel.get_unloading_consumption(duration)

    return _fuel_cost(vessel, fuel), duration


# 6. IDLE COST

def _idle_cost(vessel, duration):
    """Calculate fuel cost while the vessel is waiting."""

    duration = _check_number(
        duration,
        "Waiting time"
    )

    if duration == 0:
        return 0.0

    method = getattr(vessel, "get_idle_consumption", None)

    if not callable(method):
        raise CostEstimationError(
            "The vessel does not provide get_idle_consumption()."
        )

    fuel = method(duration)

    return _fuel_cost(vessel, fuel)


# 7. TRADE TIME WINDOWS

def _get_earliest_time(trade, is_pickup):
    """
    Get the earliest allowed arrival time for a trade event.

    MABLE TimeWindowTrade provides:
        earliest_pickup
        earliest_drop_off

    Plain trades may not provide these attributes; in that case the
    event has no time window and None is returned.
    """

    attr = "earliest_pickup" if is_pickup else "earliest_drop_off"
    earliest = getattr(trade, attr, None)

    if earliest is None:
        return None

    try:
        earliest = float(earliest)
    except (TypeError, ValueError):
        raise CostEstimationError(
            f"The trade's {attr} is not a valid number: {earliest!r}"
        )

    if not math.isfinite(earliest):
        raise CostEstimationError(
            f"The trade's {attr} must be finite."
        )

    return earliest


# 8. STANDALONE TRADE COST

def estimate_trade_cost(vessel, trade, headquarters=None) -> float:
    """
    Estimate the standalone cost of transporting one trade.

    This follows the Lab 2 and Lab 3 cost calculation:

        Loading cost
        + Unloading cost
        + Laden travel cost

    This function does not include the vessel's positioning journey
    to the pickup port. Positioning is included in the full schedule
    cost calculation.

    Parameters
    ----------
    vessel:
        The MABLE vessel carrying the trade.

    trade:
        The MABLE trade object.

    headquarters:
        Optional company headquarters. Used to calculate network
        distance if the vessel is not attached to a simulation.

    Returns
    -------
    float:
        Estimated standalone monetary cost.
    """

    if vessel is None or trade is None:
        raise CostEstimationError(
            "Both vessel and trade must be provided."
        )

    loading_cost, _ = _loading_cost(vessel, trade)
    unloading_cost, _ = _unloading_cost(vessel, trade)

    laden_cost, _ = _travel_cost(
        vessel,
        trade.origin_port,
        trade.destination_port,
        laden=True,
        headquarters=headquarters,
    )

    total_cost = loading_cost + unloading_cost + laden_cost

    return _check_number(
        total_cost,
        "Standalone trade cost"
    )


# 9. INITIAL CARGO ON THE VESSEL

def _get_initial_cargo(vessel):
    """Read current cargo (MABLE may expose cargo types as property or method)."""
    loadable = getattr(vessel, "loadable_cargo_types", None)
    current_load = getattr(vessel, "current_load", None)
    if not callable(current_load) or loadable is None:
        raise CostEstimationError(
            "Vessel must provide loadable_cargo_types and current_load()."
        )
    try:
        cargo_types = loadable() if callable(loadable) else loadable
        cargo_types = list(cargo_types)
    except (TypeError, ValueError) as exc:
        raise CostEstimationError("Invalid vessel cargo-type collection") from exc

    cargo_onboard = {}
    for cargo_type in cargo_types:
        amount = _check_number(current_load(cargo_type), "Current cargo amount")
        if amount > 0:
            cargo_onboard[cargo_type] = amount
    return cargo_onboard


# 10. COMPLETE SCHEDULE COST

def estimate_schedule_cost(
    vessel,
    schedule,
    headquarters=None,
    return_breakdown=False,
):
    """
    Estimate the total cost of carrying out a complete vessel schedule.

    The schedule is read using MABLE's get_simple_schedule() method.

    The calculation follows the ordered schedule events.

    It includes:
        - Ballast travel when the vessel is empty.
        - Laden travel when the vessel carries cargo.
        - Loading fuel.
        - Unloading fuel.
        - Idle fuel while waiting for earliest event times.

    Parameters
    ----------
    vessel:
        The MABLE vessel.

    schedule:
        The MABLE Schedule object.

    headquarters:
        Optional company headquarters.

    Returns
    -------
    float:
        Estimated total monetary cost of the schedule.
    """

    if vessel is None or schedule is None:
        raise CostEstimationError(
            "Both vessel and schedule must be provided."
        )

    get_simple_schedule = getattr(
        schedule,
        "get_simple_schedule",
        None
    )

    if not callable(get_simple_schedule):
        raise CostEstimationError(
            "The schedule does not provide get_simple_schedule()."
        )

    events = get_simple_schedule()
    # Record each cost component alongside the total. With return_breakdown=False,
    # the caller still receives a single monetary cost value.
    components = {
        "ballast": 0.0,
        "laden": 0.0,
        "loading": 0.0,
        "unloading": 0.0,
        "idle": 0.0,
        "ballast_hours": 0.0,
        "laden_hours": 0.0,
        "idle_hours": 0.0,
    }
    if not events:
        return dict(components, total=0.0) if return_breakdown else 0.0

    # Prefer an attached simulation engine, but real TradingCompany agents
    # commonly expose routing through headquarters instead.
    _, world, network = _get_simulation_context(vessel, schedule)
    if (network is None or world is None) and headquarters is None:
        raise CostEstimationError(
            "A simulation world/network or company headquarters is required "
            "to estimate the full schedule cost."
        )

    current_location = _get_current_location(vessel, world, network)

    clock = (getattr(world, "current_time", None) if world is not None
             else None)
    if clock is None and headquarters is not None:
        clock = getattr(headquarters, "current_time", None)
    if clock is None:
        raise CostEstimationError(
            "Current simulation time is unavailable from world/headquarters."
        )
    try:
        current_time = float(clock)
        schedule_head_raw = getattr(schedule, "_time_schedule_head", None)
        if schedule_head_raw is not None:
            current_time = max(current_time, float(schedule_head_raw))
    except (TypeError, ValueError) as exc:
        raise CostEstimationError(
            "The schedule or simulation has an invalid start time."
        ) from exc

    # If the vessel is on a journey and only its destination is known, model
    # it as unavailable until the currently underway voyage completes.
    journey = getattr(vessel, "location", None)
    if journey is not None and journey.__class__.__name__ == "OnJourney":
        start = getattr(journey, "start_time", None)
        origin = getattr(journey, "origin", None)
        dest = getattr(journey, "destination", None)
        if start is not None and origin is not None and dest is not None:
            try:
                underway_distance = _get_distance(
                    origin, dest, vessel, headquarters, schedule
                )
                end = float(start) + float(vessel.get_travel_time(underway_distance))
                current_time = max(current_time, end)
            except (TypeError, ValueError, AttributeError, CostEstimationError):
                # Do not silently treat a known unfinished journey as completed.
                raise CostEstimationError(
                    "Could not determine when the vessel's current journey ends."
                )

    # Current cargo onboard
    cargo_onboard = _get_initial_cargo(vessel)

    total_cost = 0.0

    # Process events in MABLE schedule order
    for event in events:

        if not isinstance(event, (tuple, list)) or len(event) != 2:
            raise CostEstimationError(
                f"Unexpected schedule event format: {event!r}"
            )

        event_type, trade = event

        # MABLE returns event names such as PICK_UP and DROP_OFF.
        event_name = getattr(
            event_type,
            "name",
            event_type
        )

        if event_name == "PICK_UP":
            is_pickup = True
            destination = trade.origin_port

        elif event_name == "DROP_OFF":
            is_pickup = False
            destination = trade.destination_port

        else:
            raise CostEstimationError(
                f"Unknown schedule event type: {event_type!r}. "
                f"Only PICK_UP and DROP_OFF events are supported."
            )

        # Determine whether the vessel is carrying cargo.
        is_laden = any(
            amount > 0
            for amount in cargo_onboard.values()
        )

        # Travel to the next event.
        travel_cost, travel_time = _travel_cost(
            vessel,
            current_location,
            destination,
            laden=is_laden,
            headquarters=headquarters,
            schedule=schedule
        )

        total_cost += travel_cost
        components["laden" if is_laden else "ballast"] += travel_cost
        components["laden_hours" if is_laden else "ballast_hours"] += travel_time
        current_time += travel_time
        current_location = destination

        # Wait if the vessel arrives before the earliest allowed time.
        earliest_time = _get_earliest_time(
            trade,
            is_pickup
        )

        if earliest_time is not None and earliest_time > current_time:

            waiting_time = earliest_time - current_time

            waiting_cost = _idle_cost(
                vessel,
                waiting_time
            )

            total_cost += waiting_cost
            components["idle"] += waiting_cost
            components["idle_hours"] += waiting_time
            current_time = earliest_time

        # Calculate cargo transfer cost.
        if is_pickup:

            transfer_cost, transfer_time = _loading_cost(
                vessel,
                trade
            )

        else:

            transfer_cost, transfer_time = _unloading_cost(
                vessel,
                trade
            )

        total_cost += transfer_cost
        components["loading" if is_pickup else "unloading"] += transfer_cost
        current_time += transfer_time

        # Update cargo onboard after the event.
        cargo_type = trade.cargo_type
        amount = _check_number(
            trade.amount,
            "Trade cargo amount"
        )

        if is_pickup:

            cargo_onboard[cargo_type] = (
                cargo_onboard.get(cargo_type, 0.0)
                + amount
            )

        else:

            remaining = (
                cargo_onboard.get(cargo_type, 0.0)
                - amount
            )

            if remaining < -0.0000001:
                raise CostEstimationError(
                    f"Schedule drops off {amount} units of "
                    f"{cargo_type!r} but only "
                    f"{cargo_onboard.get(cargo_type, 0.0)} are on board."
                )

            if remaining > 0.0000001:
                cargo_onboard[cargo_type] = remaining
            else:
                cargo_onboard.pop(cargo_type, None)

    validated_total = _check_number(total_cost, "Total schedule cost")
    if return_breakdown:
        # These are model-estimated costs, not realised simulator fuel expenses.
        return dict(components, total=validated_total)
    return validated_total


# 11. MARGINAL COST

def estimate_marginal_cost(
    vessel,
    current_schedule,
    schedule_with_trade,
    headquarters=None
) -> float:
    """
    Estimate the extra cost of adding a trade to an existing schedule.

    Formula:

        Marginal cost =
            Cost after adding trade
            - Cost before adding trade

    Parameters
    ----------
    vessel:
        The MABLE vessel.

    current_schedule:
        The vessel's existing schedule.

    schedule_with_trade:
        A copy of the schedule after adding the new trade.

    headquarters:
        Optional company headquarters.

    Returns
    -------
    float:
        Additional cost caused by the schedule change.

    Note:
        The result can be negative if the new schedule reduces the
        total estimated travel cost.
    """

    if (
        vessel is None
        or current_schedule is None
        or schedule_with_trade is None
    ):
        raise CostEstimationError(
            "Vessel and both schedules must be provided."
        )

    # Cost before adding the trade
    original_cost = estimate_schedule_cost(
        vessel,
        current_schedule,
        headquarters
    )

    # Cost after adding the trade
    updated_cost = estimate_schedule_cost(
        vessel,
        schedule_with_trade,
        headquarters
    )

    # A newly inserted trade can occasionally reduce total route cost through
    # consolidation, so the marginal cost is not required to be positive.
    marginal_cost = updated_cost - original_cost

    if not math.isfinite(marginal_cost):
        raise CostEstimationError(
            "Marginal cost calculation produced an invalid result."
        )

    return marginal_cost
# ---------------------------------------------------------------------------
# Route scheduling
# ---------------------------------------------------------------------------
# The scheduler accepts cost_fn(vessel, schedule), which returns the total
# estimated running cost of a vessel's proposed schedule. We compare the
# change from each vessel's existing commitments, not its total fleet cost.
# Every candidate insertion is checked with MABLE's verify_schedule().

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
    assignments: dict = field(default_factory=dict)  # newly awarded trade -> vessel


class SchedulingOptimizer:
    """Multi-start feasible insertion, scored using estimated *net income*.

    Pre-auction uses coverage then cost.  Post-auction payments are already
    awarded, so the income comparison between schedules is equivalent to
    minimising incremental fleet operating cost PLUS estimated penalties for
    contracts left unfulfilled. If penalty estimates cannot be obtained,
    revert to fulfilment-first ranking; never manufacture a penalty value.

    The insertion search follows the general approach of Mai et al. (AAMAS
    2025, Section 3.1, Algorithms 1-3). Our restricted-candidate sampling,
    penalty fallback, and subsequent local search are additional heuristics.
    Every trial schedule is copied and verified by MABLE.
    """

    def __init__(self, fleet, cost_fn=None, restarts=8,
                 time_budget_s=8.0, seed=42, penalty_fn=None,
                 post_rcl_size=3, local_search_passes=2,
                 local_search_max_moves=8):
        self.fleet = list(fleet)
        if cost_fn is not None and not callable(cost_fn):
            raise TypeError("cost_fn must be callable or None")
        if penalty_fn is not None and not callable(penalty_fn):
            raise TypeError("penalty_fn must be callable or None")
        self.cost_fn = cost_fn
        self.penalty_fn = penalty_fn
        self.post_rcl_size = max(1, int(post_rcl_size))
        self.local_search_passes = max(0, int(local_search_passes))
        self.local_search_max_moves = max(0, int(local_search_max_moves))
        self.restarts = max(1, int(restarts))
        self.time_budget_s = max(0.1, float(time_budget_s))
        self.seed = seed

    def _cost(self, vessel, schedule):
        value = float(self.cost_fn(vessel, schedule))
        if not isfinite(value) or value < 0:
            raise ValueError("Schedule cost must be finite and non-negative")
        return value

    def _candidates(self, base_schedule, trade, append_only):
        """Yield copied, feasible insertion alternatives only."""
        if append_only:
            trial = base_schedule.copy()
            try:
                trial.add_transportation(trade)
            except (ValueError, IndexError, AssertionError):
                return
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
                    continue
                # Never compare a route that violates capacity or time windows.
                if trial.verify_schedule():
                    yield trial

    def best_insertion(self, trade, tentative_schedules, deadline=None,
                       append_only=False):
        """Return (vessel, copied_schedule, marginal_cost), or None."""
        best = None
        for vessel in self.fleet:
            if deadline is not None and monotonic() >= deadline:
                break
            base = tentative_schedules.get(vessel, vessel.schedule)
            old_cost = self._cost(vessel, base) if self.cost_fn else 0.0
            for candidate in self._candidates(base, trade, append_only):
                extra_cost = (
                    self._cost(vessel, candidate) - old_cost
                    if self.cost_fn else 0.0
                )
                if best is None or extra_cost < best[2]:
                    best = (vessel, candidate, extra_cost)
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
        result.assignments[trade] = vessel
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

    def sample_preauction_plan(self, order, deadline, *, seed=0, run_id=1,
                               max_choices=4, cost_slack=0.60,
                               cost_floor=25.0):
        """Sample a *different*, fully verified pre-auction insertion plan.

        The normal ``plan()`` deliberately returns the cheapest/most-covered
        restart, which collapses randomized insertion runs to the same plan.
        This method instead retains a bounded set of feasible alternatives
        for each incoming trade (different vessels and, when possible,
        different pickup/drop-off positions) and samples from that set.

        A candidate is eligible for exploration only if its *incremental*
        cost lies within ``max(cost_floor, cost_slack * abs(best_cost))``
        of the cheapest move. Existing live schedules are never modified.
        The method is pre-auction only; post-auction commitments continue to
        use the original income/penalty-aware ``plan()`` algorithm.
        """
        rng = Random(seed)
        result = PlanningResult()
        for step, trade in enumerate(order):
            if monotonic() >= deadline:
                break
            best_by_vessel = []
            second_by_vessel = []
            for vessel in self.fleet:
                if monotonic() >= deadline:
                    break
                base = result.schedules.get(vessel, vessel.schedule)
                base_cost = self._cost(vessel, base) if self.cost_fn else 0.0
                # Keep at most two *structurally distinct* placements per
                # vessel, rather than choosing the same cheapest route on
                # every restart. MABLE validates each copied schedule.
                candidates = []
                seen = set()
                for candidate in self._candidates(base, trade, append_only=False):
                    if monotonic() >= deadline:
                        break
                    signature = tuple(
                        (getattr(kind, "name", kind), id(t))
                        for kind, t in candidate.get_simple_schedule()
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    extra = (self._cost(vessel, candidate) - base_cost
                             if self.cost_fn else 0.0)
                    if not isfinite(extra):
                        continue
                    candidates.append((vessel, candidate, extra))
                    candidates.sort(key=lambda choice: choice[2])
                    if len(candidates) > 2:
                        candidates.pop()
                if candidates:
                    best_by_vessel.append(candidates[0])
                if len(candidates) > 1:
                    second_by_vessel.append(candidates[1])

            if not best_by_vessel:
                continue
            best_by_vessel.sort(key=lambda choice: choice[2])
            cheapest = best_by_vessel[0][2]
            ceiling = cheapest + max(float(cost_floor),
                                     float(cost_slack) * abs(cheapest))
            primary = [move for move in best_by_vessel if move[2] <= ceiling]
            secondary = [move for move in second_by_vessel if move[2] <= ceiling]
            secondary.sort(key=lambda choice: choice[2])
            pool = (primary + secondary)[:max(1, int(max_choices))]
            if step == 0:
                # Different run IDs explore different vessel assignments at
                # the first opportunity, even when trade order is the same.
                selected = primary[(run_id - 1) % len(primary)]
            elif len(pool) > 1 and rng.random() < 0.55:
                selected = rng.choice(pool[1:])
            else:
                selected = pool[0]
            self._assign(result, trade, selected)
        return result

    def _post_auction_run(self, order, payments, deadline,
                          penalties=None, rng=None, rcl_size=1):
        """Construct a revenue/cost-aware post-award schedule.

        With penalty estimates, priority is penalty avoided minus insertion
        cost; awarded payments are already fixed across the candidate plans.
        Without penalty estimates, use the payment-minus-cost insertion rule
        described by Mai et al. (AAMAS 2025, Section 3.3).

        Do not drop feasible awards just because they have a negative gain:
        unfulfilled contracts may carry an additional penalty. RCL
        randomisation explores alternative insertion orders on restarts.
        """
        result = PlanningResult()
        remaining = list(order)
        while remaining and monotonic() < deadline:
            candidates = []
            for trade in remaining:
                if monotonic() >= deadline:
                    break
                choice = self.best_insertion(trade, result.schedules, deadline)
                if choice is None:
                    continue
                priority = (
                    penalties[trade] - choice[2]
                    if penalties is not None
                    else float(payments.get(trade, 0.0)) - choice[2]
                )
                # Earlier pickup as a tie-breaker, where available.
                candidates.append((priority, trade, choice))
            if not candidates:
                break
            candidates.sort(key=lambda x: x[0], reverse=True)
            limit = min(max(1, rcl_size), len(candidates))
            selected = candidates[0] if limit == 1 else rng.choice(candidates[:limit])
            _, trade, choice = selected
            self._assign(result, trade, choice)
            remaining.remove(trade)
        return result

    @staticmethod
    def _latest_pickup(trade):
        value = getattr(trade, "latest_pickup", None)
        return float("inf") if value is None else value

    def _penalty_estimates(self, trades):
        """Return penalty mapping or None if estimates are unavailable.

        Never use an arbitrary default penalty: an incorrect estimate can
        make missing an award appear cheaper than delivering it.
        """
        if self.penalty_fn is None:
            return None
        estimates = {}
        for trade in trades:
            try:
                penalty = float(self.penalty_fn(trade))
            except Exception:
                return None
            if not isfinite(penalty) or penalty < 0:
                return None
            estimates[trade] = penalty
        return estimates

    def _quality(self, result, payments, penalties=None, awarded=None):
        """Larger is better; use full income when penalties are estimable."""
        if payments is None:
            return (len(result.scheduled_trades), -result.total_extra_cost)
        if penalties is None:
            # Unknown non-delivery penalty: safeguard fulfilment first.
            return (len(result.scheduled_trades), -result.total_extra_cost)
        scheduled = set(result.scheduled_trades)
        missing = [t for t in awarded if t not in scheduled]
        gross_revenue = sum(float(payments.get(t, 0.0)) for t in awarded)
        proxy_income = (gross_revenue - result.total_extra_cost
                        - sum(penalties[t] for t in missing))
        return (proxy_income, len(result.scheduled_trades),
                -result.total_extra_cost)

    def _route_order(self, result, vessel):
        """Newly awarded trades in pickup order; existing commitments stay fixed.

        Only trades in ``result.assignments`` may move. Existing vessel.schedule
        events are never removed or edited during local search.
        """
        owned = {t for t, v in result.assignments.items() if v is vessel}
        if not owned:
            return []
        ordered = []
        try:
            schedule = result.schedules.get(vessel, vessel.schedule)
            for kind, trade in schedule.get_simple_schedule():
                name = getattr(kind, "name", kind)
                if name == "PICK_UP" and trade in owned and trade not in ordered:
                    ordered.append(trade)
        except (AttributeError, ValueError, TypeError):
            pass
        # Some mock schedules have a different event representation. Preserve
        # their original insertion order rather than inferring a wrong route.
        ordered.extend(t for t in result.scheduled_trades
                       if t in owned and t not in ordered)
        return ordered

    def _rebuild_vessel(self, vessel, ordered_trades, deadline):
        """Reinsert *new* trades on a copy of the unchanged live schedule.

        Return (schedule, marginal_cost, per_trade_marginals), or None when
        time expires or one of the insertions cannot be verified by MABLE.
        """
        if monotonic() >= deadline:
            return None
        trial = vessel.schedule.copy()
        original_cost = self._cost(vessel, trial)
        previous_cost = original_cost
        trade_costs = {}
        for trade in ordered_trades:
            if monotonic() >= deadline:
                return None
            cheapest = None
            for candidate in self._candidates(trial, trade, append_only=False):
                if monotonic() >= deadline:
                    return None
                candidate_cost = self._cost(vessel, candidate)
                if cheapest is None or candidate_cost < cheapest[1]:
                    cheapest = (candidate, candidate_cost)
            if cheapest is None:
                return None
            trial, current_cost = cheapest
            trade_costs[trade] = current_cost - previous_cost
            previous_cost = current_cost
        return trial, previous_cost - original_cost, trade_costs

    def _apply_local_move(self, result, changes, new_assignments):
        """Create a new plan; never mutate the current best or live schedules."""
        updated = PlanningResult(
            schedules=dict(result.schedules),
            scheduled_trades=list(result.scheduled_trades),
            costs=dict(result.costs),
            total_extra_cost=result.total_extra_cost,
            assignments=dict(result.assignments),
        )
        old_sum = 0.0
        new_sum = 0.0
        for vessel, (schedule, delta, per_trade) in changes.items():
            old_schedule = result.schedules.get(vessel, vessel.schedule)
            old_sum += self._cost(vessel, old_schedule) - self._cost(vessel, vessel.schedule)
            new_sum += delta
            # No newly awarded trades remain on this vessel: keep the original
            # vessel schedule rather than submitting a redundant replacement.
            if any(v is vessel for v in new_assignments.values()):
                updated.schedules[vessel] = schedule
            else:
                updated.schedules.pop(vessel, None)
            for trade, owner in result.assignments.items():
                if owner is vessel:
                    updated.costs.pop(trade, None)
            updated.costs.update(per_trade)
        updated.assignments = new_assignments
        updated.total_extra_cost += new_sum - old_sum
        return updated

    def _local_improve(self, result, deadline):
        """Bounded first-improvement local search on post-auction awards.

        Try single-trade transfers between vessels and adjacent pickup-order
        swaps on a vessel. Rebuild affected routes with feasible insertion,
        accepting only strict marginal-cost reductions with unchanged contract
        coverage. No speculative edits to current vessel.schedule are made.
        """
        if (self.cost_fn is None or self.local_search_passes == 0
                or self.local_search_max_moves == 0 or not result.assignments):
            return result
        start_cost = result.total_extra_cost
        accepted = 0
        candidates_tested = 0
        feasible_candidates = 0
        for _ in range(self.local_search_passes):
            if monotonic() >= deadline or accepted >= self.local_search_max_moves:
                break
            improved = False
            orders = {v: self._route_order(result, v) for v in self.fleet}
            # Consider higher incremental-cost awards first, without dropping them.
            trades = sorted(result.assignments,
                            key=lambda t: result.costs.get(t, 0.0), reverse=True)
            for trade in trades:
                if monotonic() >= deadline:
                    break
                src = result.assignments[trade]
                src_order = [t for t in orders[src] if t != trade]
                src_rebuilt = self._rebuild_vessel(src, src_order, deadline)
                if src_rebuilt is None:
                    continue
                for dst in self.fleet:
                    if monotonic() >= deadline:
                        break
                    if dst is src:
                        continue
                    dst_order = orders[dst]
                    # Both insertion orders can produce different feasible
                    # consolidated routes even though each uses best insertion.
                    variants = [dst_order + [trade]]
                    if dst_order:
                        variants.append([trade] + dst_order)
                    for order in variants:
                        candidates_tested += 1
                        rebuilt = self._rebuild_vessel(dst, order, deadline)
                        if rebuilt is None:
                            continue
                        feasible_candidates += 1
                        candidate_assignments = dict(result.assignments)
                        candidate_assignments[trade] = dst
                        proposed = self._apply_local_move(
                            result, {src: src_rebuilt, dst: rebuilt},
                            candidate_assignments,
                        )
                        if proposed.total_extra_cost < result.total_extra_cost - 1e-6:
                            result = proposed
                            accepted += 1
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
            if not improved:
                # Within-vessel reorder: swap adjacent pickup order and find
                # a new minimum-cost insertion plan on that vessel.
                for vessel, order in orders.items():
                    if monotonic() >= deadline:
                        break
                    for idx in range(len(order) - 1):
                        swapped = order.copy()
                        swapped[idx], swapped[idx + 1] = swapped[idx + 1], swapped[idx]
                        candidates_tested += 1
                        rebuilt = self._rebuild_vessel(vessel, swapped, deadline)
                        if rebuilt is None:
                            continue
                        feasible_candidates += 1
                        proposed = self._apply_local_move(
                            result, {vessel: rebuilt}, dict(result.assignments),
                        )
                        if proposed.total_extra_cost < result.total_extra_cost - 1e-6:
                            result = proposed
                            accepted += 1
                            improved = True
                            break
                    if improved:
                        break
            if not improved:
                break
        # Log zero-improvement rounds too, so search effort can be compared
        # with the savings achieved without affecting route submission.
        logger.info(
            "LOCAL_SEARCH: accepted_moves={} candidates_tested={} "
            "feasible_candidates={} incremental_cost_before={:.2f} "
            "incremental_cost_after={:.2f} saving={:.2f} time_exhausted={}",
            accepted, candidates_tested, feasible_candidates,
            start_cost, result.total_extra_cost,
            start_cost - result.total_extra_cost, monotonic() >= deadline,
        )
        return result

    def plan(self, trades, payments=None):
        """Return the best verified plan found within the time budget.

        `payments` must contain *awarded* contract payments, not bid guesses.
        Existing vessel schedule costs are subtracted via marginal costing.
        """
        trades = list(trades)
        if not trades:
            return PlanningResult()
        deadline = monotonic() + self.time_budget_s
        # Reserve a small part of the deadline for local route improvements;
        # use the remaining time to construct complete candidate schedules.
        do_local = (payments is not None and self.cost_fn is not None
                    and len(trades) >= 2 and self.time_budget_s >= 3.0
                    and self.local_search_passes > 0 and self.local_search_max_moves > 0)
        local_reserve = min(1.5, 0.10 * self.time_budget_s) if do_local else 0.0
        construction_deadline = deadline - local_reserve
        rng = Random(self.seed)
        penalties = self._penalty_estimates(trades) if payments is not None else None

        # Start with a fast feasible schedule before exploring costlier insertions.
        best = self._one_run(trades, construction_deadline, append_only=True)
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

        for run, order in enumerate(orders[:self.restarts]):
            if monotonic() >= construction_deadline:
                break
            if payments is None:
                candidate = self._one_run(order, construction_deadline, append_only=False)
            else:
                candidate = self._post_auction_run(
                    order, payments, construction_deadline, penalties=penalties,
                    rng=rng, rcl_size=(1 if run == 0 else self.post_rcl_size),
                )
            if self._quality(candidate, payments, penalties, trades) > \
                    self._quality(best, payments, penalties, trades):
                best = candidate

        # Try to recover missing awards without altering existing commitments.
        # Estimated penalty exposure determines whether an extra route is worthwhile.
        assigned = set(best.scheduled_trades)
        for trade in trades:
            if monotonic() >= construction_deadline:
                break
            if trade in assigned:
                continue
            choice = self.best_insertion(trade, best.schedules, construction_deadline)
            if choice is None:
                continue
            # A positive expected penalty-saving justifies this insertion;
            # otherwise retain the income-optimal partial plan. Without a
            # reliable penalty estimate, always fulfil feasible commitments.
            if (payments is not None and penalties is not None
                    and choice[2] > penalties[trade]):
                continue
            self._assign(best, trade, choice)
            assigned.add(trade)
        if do_local and monotonic() < deadline:
            best = self._local_improve(best, deadline)
        return best
# ---------------------------------------------------------------------------
# Future opportunities and opponent modelling
# ---------------------------------------------------------------------------
# pre_inform() records upcoming cargo. inform() snapshots rival positions and
# submits bids; receive() updates market observations after the auction.
# Future-trade value can lower a bid, but uncertain price forecasts are kept
# separate from live bid pricing. All rival information comes from public data.
# In the reverse second-price auction, a winning company's payment represents
# the runner-up bid, not the winning company's own submitted bid.

import statistics
import math


class FuturePlanningMixin:

    # Limit how strongly speculative future work can discount a current bid.
    # These values balance future positioning against immediate cost exposure.
    FUTURE_WEIGHT = 0.55        # discounted value of credible future savings
    FUTURE_CAP_FRACTION = 0.18  # protect current-contract economics
    FUTURE_TOP_K = 2            # count the best K follow-on trades (decaying)
    FUTURE_DECAY = 0.5          # weight of the 2nd-best, 3rd-best, ... trade
    DEFAULT_MARKET_FACTOR = 1.2 # assumed competitor markup before we have data
    MIN_SAMPLES = 3             # ledger samples needed before trusting a factor
    FACTOR_CLIP = (0.5, 5.0)    # discard silly ratios (bad cost guesses)

    # ------------------------------------------------------------------ state
    def _model_init(self):
        if getattr(self, "_model_ready", False):
            return
        self._model_ready = True
        self._future_trades = []          # trades of NEXT month's auction
        self._future_auction_time = None
        self._model_dist_cache = {}
        self._model_comp_snapshot = {}       # company name -> [(vessel, location)]
        self._model_ratios = {}              # company name -> [payment / est. cost]
        self._model_market_ratios = []       # all companies pooled
        self._model_our_bids = {}            # trade key -> (our cost, our bid)
        self._model_lost = []                # (payment / our cost) on trades we lost
        self._model_won = []                 # (payment / our cost) on trades we won
        # Predictions made *before* a round, evaluated against realised
        # second-price payments on OUR wins only. An opponent-win payment
        # is the runner-up bid, not the price we would have received.
        self._model_price_predictions = {}  # trade key -> (raw forecast, opponent name)
        self._model_price_ratios = []        # realised payment / raw forecast
        self._model_price_ape = []           # absolute percentage error (realised denominator)
        # Only on OUR wins does the second-price payment correspond to the
        # cheapest competing bid. Rival-win payments are a *different* bid.
        self._model_second_price_ratios = []  # payment / min estimated competitor cost
        self._model_future_context = {}       # cached comparison per announced future trade

    # ======================================================== pre_inform
    def pre_inform(self, trades, time):
        """Called by the market before inform(): `trades` will be auctioned at `time`."""
        self._model_init()
        self._future_trades = list(trades) if trades else []
        self._future_auction_time = time
        self._model_future_context.clear()

    # ============================================================ helpers
    @staticmethod
    def _model_key(trade):
        """Stable key for a trade, so dicts don't depend on object identity."""
        return (str(trade.origin_port), str(trade.destination_port),
                round(float(trade.amount), 3),
                getattr(trade, "earliest_pickup", None),
                getattr(trade, "latest_drop_off", None))

    def _model_distance(self, a, b):
        key = (str(a), str(b))
        if key not in self._model_dist_cache:
            try:
                d = float(self.headquarters.get_network_distance(a, b))
                if not math.isfinite(d) or d < 0:
                    d = float("inf")
            except (TypeError, ValueError, AttributeError):
                d = float("inf")   # unreachable / unknown pair
            self._model_dist_cache[key] = d
        return self._model_dist_cache[key]

    @staticmethod
    def _model_ballast_cost(vessel, distance):
        """Fuel cost of sailing `distance` NM empty."""
        if distance == float("inf"):
            return float("inf")
        if distance <= 0:
            return 0.0
        t = vessel.get_travel_time(distance)
        return vessel.get_cost(vessel.get_ballast_consumption(t, vessel.speed))

    def _model_standalone_cost(self, vessel, trade, start_location=None):
        """Lab 3 Ex 3 style cost: reach origin + load + laden trip + unload.
        Used to cost a COMPETITOR's vessel. Our fleet uses the full schedule cost estimator."""
        start = start_location if start_location is not None else vessel.location
        ballast = self._model_ballast_cost(vessel, self._model_distance(start, trade.origin_port))
        load_t = vessel.get_loading_time(trade.cargo_type, trade.amount)
        load = vessel.get_cost(vessel.get_loading_consumption(load_t))
        unload = vessel.get_cost(vessel.get_unloading_consumption(load_t))
        laden_d = self._model_distance(trade.origin_port, trade.destination_port)
        laden_t = vessel.get_travel_time(laden_d)
        laden = vessel.get_cost(vessel.get_laden_consumption(laden_t, vessel.speed))
        return ballast + load + laden + unload

    def _model_end_location(self, vessel):
        """Where `vessel` will be after its current schedule finishes."""
        try:
            simple = vessel.schedule.get_simple_schedule()
            if simple:
                action, trade = simple[-1]
                return (trade.destination_port if action == "DROP_OFF"
                        else trade.origin_port)
        except Exception:
            pass
        return vessel.location

    @staticmethod
    def _model_can_carry(vessel, trade):
        """Support MABLE capacity APIs; do not assume an unknown vessel can carry."""
        for attr in ("get_max_capacity", "capacity"):
            method = getattr(vessel, attr, None)
            if callable(method):
                try:
                    return float(method(trade.cargo_type)) >= float(trade.amount)
                except (TypeError, ValueError, AttributeError):
                    continue
        fallback = getattr(vessel, "hold_capacity", None)
        if fallback is not None:
            return float(fallback) >= float(trade.amount)
        return False

    # ===================================== Lab 3 Ex 1: distance to next cargo
    def closest_future_trade(self, trade):
        """(future trade, distance NM) nearest to where `trade` drops off."""
        self._model_init()
        best, best_d = None, float("inf")
        for f in self._future_trades:
            d = self._model_distance(trade.destination_port, f.origin_port)
            if d < best_d:
                best, best_d = f, d
        return best, best_d

    # ========================= Lab 3 Ex 2: competitors' closest vessels
    def snapshot_competitors(self):
        """Record competitor vessel positions BEFORE the auction. Call in inform().
        receive() runs after vessels may have moved, so we cost from this snapshot."""
        self._model_init()
        snap = {}
        for company in self.headquarters.get_companies():
            if company.name == self.name:
                continue
            snap[company.name] = [(v, v.location) for v in company.fleet]
        self._model_comp_snapshot = snap
        # Vessel locations changed since the last auction; invalidate forecasts.
        self._model_future_context.clear()

    def find_closest_competitor_vessels(self, trade):
        """{company name: (vessel, distance NM to trade origin)} for every competitor."""
        self._model_init()
        if not self._model_comp_snapshot:
            self.snapshot_competitors()
        result = {}
        for name, vessels in self._model_comp_snapshot.items():
            best = None
            for v, loc in vessels:
                if not self._model_can_carry(v, trade):
                    continue
                d = self._model_distance(loc, trade.origin_port)
                if best is None or d < best[1]:
                    best = (v, d)
            if best is not None:
                result[name] = best
        return result

    def competitor_costs(self, trade):
        """{company name: estimated cheapest standalone cost for that company}."""
        self._model_init()
        if not self._model_comp_snapshot:
            self.snapshot_competitors()
        out = {}
        for name, vessels in self._model_comp_snapshot.items():
            costs = []
            for v, loc in vessels:
                try:
                    if not self._model_can_carry(v, trade):
                        continue
                    value = float(self._model_standalone_cost(v, trade, loc))
                    if math.isfinite(value) and value > 0:
                        costs.append(value)
                except (TypeError, ValueError, AttributeError, OverflowError):
                    # An unusual competitor vessel must not break our bids.
                    continue
            if costs:
                out[name] = min(costs)
        return out

    # ================================================ future-opportunity score
    def _model_future_baseline(self, future):
        """Cheapest *compatible vessel's* empty repositioning cost to future.

        Evaluate each compatible vessel's own fuel rate. Distance alone is
        insufficient when vessels have different consumption characteristics.
        """
        self._model_init()
        key = self._model_key(future)
        if key in self._model_future_context:
            return self._model_future_context[key]
        baseline = float("inf")
        for candidate in self.fleet:
            try:
                if not self._model_can_carry(candidate, future):
                    continue
                distance = self._model_distance(
                    self._model_end_location(candidate), future.origin_port,
                )
                value = float(self._model_ballast_cost(candidate, distance))
                if math.isfinite(value) and value >= 0:
                    baseline = min(baseline, value)
            except (TypeError, ValueError, AttributeError, OverflowError):
                continue
        # Use pre-auction opponent positions only as a conservative proxy;
        # these are NOT live positions at next month's auction.
        try:
            competitor_costs = self.competitor_costs(future)
            rival_cost = min(competitor_costs.values(), default=None)
            rival_distances = self.find_closest_competitor_vessels(future)
            rival_distance = min((d for _, d in rival_distances.values()),
                                 default=float("inf"))
        except (TypeError, ValueError, AttributeError, OverflowError):
            rival_cost = None
            rival_distance = float("inf")
        context = baseline, rival_cost, rival_distance
        self._model_future_context[key] = context
        return context

    def future_trade_bonus(self, vessel, trade, cost=None, schedule=None):
        """Conservative value of reaching a useful port for a *future* auction.

        AAMAS-style robust pricing motivates not relying on one optimistic
        scenario. Here an opportunity is valued at plausible repositioning
        savings times a conservative chance of winning that future request.
        Both relative voyage *cost* and distance have to favour us; neither
        noisy second-price observations nor unverified future revenue enters
        the bid. This is an adaptation, not an algorithm from the paper.
        """
        self._model_init()
        if not self._future_trades:
            return 0.0
        try:
            end_time = schedule.completion_time() if schedule is not None else None
        except (TypeError, ValueError, AttributeError):
            end_time = None

        values = []
        for future in self._future_trades:
            try:
                if not self._model_can_carry(vessel, future):
                    continue
                distance = self._model_distance(trade.destination_port,
                                             future.origin_port)
                if not math.isfinite(distance):
                    continue
                if end_time is not None:
                    latest = getattr(future, "latest_pickup", None)
                    if latest is not None and (end_time + vessel.get_travel_time(distance) > latest):
                        continue
                baseline, rival_cost, rival_distance = self._model_future_baseline(future)
                if not math.isfinite(baseline):
                    # Unknown baseline isn't evidence of an opportunity.
                    continue
                new_ballast = float(self._model_ballast_cost(vessel, distance))
                saving = baseline - new_ballast
                if not math.isfinite(saving) or saving <= 0:
                    continue

                if rival_cost is None or not math.isfinite(rival_distance):
                    # No comparable rival is observable, but their future
                    # deployment is still uncertain: don't assume a 90% win.
                    p_win = 0.45
                else:
                    our_future_cost = float(self._model_standalone_cost(
                        vessel, future, start_location=trade.destination_port,
                    ))
                    if not math.isfinite(our_future_cost) or our_future_cost < 0:
                        continue
                    # Both proxies are monotone: lower cost / shorter empty
                    # journeys improve competitiveness. Taking the minimum
                    # guards against overconfidence in either alone.
                    cost_advantage = rival_cost / max(1e-9, rival_cost + our_future_cost)
                    distance_advantage = rival_distance / max(
                        1e-9, rival_distance + distance,
                    )
                    p_win = min(cost_advantage, distance_advantage)
                    p_win = max(0.05, min(0.70, p_win))
                values.append(p_win * saving)
            except (TypeError, ValueError, AttributeError, OverflowError):
                # The future opportunity is optional: never block real bids.
                continue

        if not values:
            return 0.0
        values.sort(reverse=True)
        credible_saving = sum(
            value * self.FUTURE_DECAY ** index
            for index, value in enumerate(values[:self.FUTURE_TOP_K])
        )
        bonus = max(0.0, self.FUTURE_WEIGHT * credible_saving)
        if cost is not None:
            try:
                current_cost = float(cost)
                if not math.isfinite(current_cost) or current_cost <= 0:
                    return 0.0
                bonus = min(bonus, self.FUTURE_CAP_FRACTION * current_cost)
            except (TypeError, ValueError):
                return 0.0
        return float(bonus)

    # =================================== Lab 3 Ex 3: auction-outcome analysis
    def record_our_bid(self, trade, cost, bid):
        """Cache submitted bid values to compare with the auction ledger."""
        self._model_init()
        self._model_our_bids[self._model_key(trade)] = (cost, bid)

    def record_market_prediction(self, trade, predicted, competitor=None):
        """Save a pre-auction price prediction for diagnostics, not bid selection."""
        self._model_init()
        if predicted is None:
            return
        try:
            value = float(predicted)
        except (TypeError, ValueError):
            return
        if math.isfinite(value) and value > 0:
            self._model_price_predictions[self._model_key(trade)] = (value, competitor)

    def market_price_calibration(self):
        """Robust retrospective correction; NOT applied to submitted bids.

        At least MIN_SAMPLES observed wins are needed to avoid reacting to one
        noisy auction. The rolling median resists extreme ratios.
        """
        self._model_init()
        ratios = self._model_price_ratios
        return (statistics.median(ratios)
                if len(ratios) >= self.MIN_SAMPLES else 1.0)

    def update_opponent_models(self, auction_ledger):
        """Update the price diagnostics from the latest auction ledger.

        auction_ledger = {company name: [Contract(trade, payment), ...]}
        """
        self._model_init()
        if not auction_ledger:
            # Keep earlier samples and wait for a usable ledger.
            self._model_price_predictions.clear()
            self._model_our_bids.clear()
            return
        round_price_samples = 0
        for name, contracts in auction_ledger.items():
            for c in contracts or []:
                trade, payment = c.trade, c.payment
                key = self._model_key(trade)
                ours = self._model_our_bids.get(key)

                if name == self.name:
                    if ours and ours[0] > 0:
                        self._model_won.append(payment / ours[0])
                    # When WE win a reverse second-price auction, payment is
                    # the lowest competing bid. Learn its scale from those
                    # outcomes only. A rival's payment is NOT its own bid.
                    try:
                        competitors = self.competitor_costs(trade)
                        cheapest = min(competitors.values(), default=None)
                        realised = float(payment)
                        if cheapest is not None and cheapest > 0 and realised > 0:
                            ratio = realised / cheapest
                            if math.isfinite(ratio) and 0.1 <= ratio <= 10.0:
                                self._model_second_price_ratios.append(ratio)
                                del self._model_second_price_ratios[:-80]
                    except (TypeError, ValueError, OverflowError, AttributeError):
                        pass
                    forecast = self._model_price_predictions.get(key)
                    if forecast is not None:
                        predicted, _predicted_competitor = forecast
                        try:
                            realised = float(payment)
                            ratio = realised / predicted
                            if (math.isfinite(realised) and realised > 0
                                    and math.isfinite(ratio) and 0.02 <= ratio <= 50.0):
                                self._model_price_ratios.append(ratio)
                                self._model_price_ape.append(abs(predicted - realised) / realised)
                                # Bounded memory and stable per-round processing.
                                del self._model_price_ratios[:-100]
                                del self._model_price_ape[:-100]
                                round_price_samples += 1
                                logger.info(
                                    "PRICE_AUDIT: predicted={:.2f} realised={:.2f} "
                                    "ratio={:.3f} abs_pct_error={:.1f}%",
                                    predicted, realised, ratio,
                                    100.0 * abs(predicted - realised) / realised,
                                )
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                    continue

                # A competitor won. Estimate their cost from the pre-auction snapshot.
                vessels = self._model_comp_snapshot.get(name)
                if vessels:
                    costs = [self._model_standalone_cost(v, trade, loc)
                             for v, loc in vessels if self._model_can_carry(v, trade)]
                    costs = [x for x in costs if 0 < x < float("inf")]
                    if costs:
                        r = payment / min(costs)
                        if self.FACTOR_CLIP[0] <= r <= self.FACTOR_CLIP[1]:
                            self._model_ratios.setdefault(name, []).append(r)
                            self._model_market_ratios.append(r)

                if ours and ours[0] > 0:
                    self._model_lost.append(payment / ours[0])
        self._model_our_bids = {}
        self._model_price_predictions = {}
        if round_price_samples:
            logger.info(
                "PRICE_CALIBRATION: observed_wins={} total_samples={} "
                "median_observed_over_predicted={:.3f} median_abs_pct_error={:.1f}% "
                "second_price_samples={} second_price_cost_factor={:.3f} "
                "(diagnostic_only=True)",
                round_price_samples, len(self._model_price_ratios),
                self.market_price_calibration(),
                100.0 * statistics.median(self._model_price_ape),
                len(self._model_second_price_ratios), self._model_second_price_factor(),
            )

    def competitor_factor(self, name):
        """Learned payment/cost factor for one competitor (median, robust to outliers)."""
        self._model_init()
        r = self._model_ratios.get(name, [])
        if len(r) >= self.MIN_SAMPLES:
            return statistics.median(r)
        return self.market_factor()

    def market_factor(self):
        self._model_init()
        if len(self._model_market_ratios) >= self.MIN_SAMPLES:
            return statistics.median(self._model_market_ratios)
        return self.DEFAULT_MARKET_FACTOR

    def _model_second_price_factor(self):
        """Shrunk robust multiplier learned from our own second-price wins.

        Start with a prior rather than jumping to a multiplier from just one
        auction. Wins are selected samples, so even with history the estimate
        stays conservative and is for diagnostics only.
        """
        self._model_init()
        samples = self._model_second_price_ratios
        if len(samples) < self.MIN_SAMPLES:
            return self.DEFAULT_MARKET_FACTOR
        # The median in log space reduces the influence of very large prices;
        # the bounded weight avoids overfitting a few won contracts.
        logs = [math.log(value) for value in samples if value > 0]
        if not logs:
            return self.DEFAULT_MARKET_FACTOR
        weight = min(0.75, len(logs) / (len(logs) + 8.0))
        learned = math.exp(
            (1.0 - weight) * math.log(self.DEFAULT_MARKET_FACTOR)
            + weight * statistics.median(logs)
        )
        return max(0.5, min(2.0, learned))

    def estimated_competitor_bid(self, trade):
        """Forecast cheapest rival bid, based on our own second-price wins.

        Diagnostic only: do not feed uncertain market-price predictions
        straight into submitted bids or speculative future discounts.
        """
        costs = self.competitor_costs(trade)
        if not costs:
            return None, None
        who = min(costs, key=costs.get)
        return costs[who] * self._model_second_price_factor(), who

    def expected_profit(self, trade, our_cost):
        """Predicted payment minus our cost (None if no competitor can carry it)."""
        bid, _ = self.estimated_competitor_bid(trade)
        return None if bid is None else bid - our_cost

    def lost_margin_hint(self):
        """Median of (payment / our cost) on trades we LOST. Below ~1.0 means
        competitors clear below our cost: our cost model or markup is too high."""
        self._model_init()
        return statistics.median(self._model_lost) if self._model_lost else None

    def opponent_summary(self):
        """Learned figures for logging and for the report."""
        self._model_init()
        return {
            "per_company_factor": {n: round(statistics.median(r), 3)
                                   for n, r in self._model_ratios.items() if r},
            "samples": {n: len(r) for n, r in self._model_ratios.items()},
            "market_factor": round(self.market_factor(), 3),
            "won_payment_over_cost": (round(statistics.median(self._model_won), 3)
                                      if self._model_won else None),
            "lost_payment_over_cost": (round(self.lost_margin_hint(), 3)
                                       if self._model_lost else None),
            "second_price_samples": len(self._model_second_price_ratios),
            "second_price_cost_factor": round(self._model_second_price_factor(), 3),
            "price_calibration_samples": len(self._model_price_ratios),
            "price_calibration_factor": round(self.market_price_calibration(), 3),
            "price_median_absolute_percentage_error": (
                round(statistics.median(self._model_price_ape), 3)
                if self._model_price_ape else None
            ),
        }

# ---------------------------------------------------------------------------
# Pre-auction bidding
# ---------------------------------------------------------------------------
# Construct distinct feasible plans, rank them by route consolidation and
# added ballast, and estimate how consistently each trade appears. Selected
# trades use a 50/50 blend of dedicated cost and shared-route allocation.
# The route-sharing approach is adapted from Mai et al. (AAMAS 2025, §3.2).

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import random
from time import monotonic

from loguru import logger
from mable.transport_operation import Bid

# Bound the number of pre-auction searches to fit the auction callback budget.
# Candidate schedules are validated, so each restart can be comparatively costly.
P_SELECT = 0.60
PRE_RESTARTS = 12
PRE_TOP_K = 8
INFORM_BUDGET_S = 12.0
MIN_BID = 0.01
RANDOM_SEED = 6203
# Prefer consolidated routes with less additional empty repositioning.
# The ballast coefficient is a local tuning choice, not a paper parameter.
BALLAST_PENALTY_WEIGHT = 0.25
# Retained plans vote in proportion to route quality, with a floor to preserve
# alternatives. This extends AAMAS-style plan ranking; it is not the paper's
# exact trade-selection probability calculation.
PLAN_QUALITY_STRENGTH = 1.00
PLAN_MIN_RELATIVE_WEIGHT = 0.50


@dataclass
class _CandidatePlan:
    signature: tuple
    assignments: dict  # id(trade) -> vessel
    allocation: dict  # id(trade) -> allocated fuel cost
    coverage: int
    efficiency: float
    ballast_delta: float = 0.0  # candidate ballast cost - live-schedule ballast cost
    ballast_fraction: float = 0.0  # positive additional ballast / candidate route cost


class BiddingStrategy:
    """Compute bids independently of the company's receive() implementation.

    Expected `company` API: .fleet, .headquarters.get_network_distance(...),
    and optionally .estimate_schedule_cost(vessel, schedule).
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
        self.ballast_penalty_weight = BALLAST_PENALTY_WEIGHT
        self.plan_quality_strength = PLAN_QUALITY_STRENGTH
        self._baseline_ballast_cache = {}  # key is live vessel identity, one auction round

    def _rank_score(self, plan):
        """Consolidation efficiency with an additional ballast-cost penalty.

        The efficiency basis follows Mai et al. (AAMAS 2025, Section 3.1).
        The incremental-ballast penalty is specific to this implementation.

        Efficient consolidation remains the dominant selection criterion.
        Live committed ballast is not charged again for each proposed plan.
        """
        return plan.efficiency - self.ballast_penalty_weight * plan.ballast_fraction

    def _plan_weights(self, plans):
        """Give higher-quality plans more influence, without discarding diversity.

        This weighting is an extension of AAMAS top-K ranking, not the
        paper's unweighted appearance-frequency calculation. Scores combine
        consolidation efficiency with the existing
        incremental-ballast penalty. Exponential weights depend on the actual
        quality gap, not arbitrary ranking positions. A 50% raw-weight floor
        prevents a single exploratory plan from being effectively ignored.
        Normalise to mean weight 1.0; equal scores preserve old frequency and
        allocated-cost calculations exactly, as does having only one plan.
        """
        if not plans:
            return []
        scores = [self._rank_score(plan) for plan in plans]
        if not all(math.isfinite(score) for score in scores):
            logger.warning("PRE_PLAN_WEIGHTS: invalid plan score; using equal weights")
            return [1.0] * len(plans)
        best = max(scores)
        raw = [
            max(
                PLAN_MIN_RELATIVE_WEIGHT,
                math.exp(-min(20.0, max(0.0, self.plan_quality_strength * (best - score)))),
            )
            for score in scores
        ]
        normaliser = len(raw) / sum(raw)
        return [weight * normaliser for weight in raw]

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
        """Return the fleet cost estimator's direct trade cost if available."""
        trade_cost_fn = getattr(self.company, "estimate_trade_cost", None)
        if callable(trade_cost_fn):
            cost = float(trade_cost_fn(vessel, trade))
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid standalone trade cost")
            return cost
        return (
            self._sailing_cost(
                vessel, self._distance(trade.origin_port, trade.destination_port), True
            )
            + self._transfer_cost(vessel, trade, True)
            + self._transfer_cost(vessel, trade, False)
        )

    def _dedicated_cost(self, vessel, trade):
        """Dedicated-trip cost from current commitments through pickup and delivery.

        Corresponds to the absolute-cost term in Mai et al., Section 3.2.
        """
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

    def _route_allocation(self, vessel, schedule, *, with_ballast=False):
        """Allocate shared sailing-leg costs to the trades using those legs.

        Based on the proportional allocation in Mai et al. (AAMAS 2025,
        Section 3.2); this implementation shares pickup detours with the
        newly collected trade as well as cargo already onboard.

        Include the incoming pickup in a sailing leg's beneficiaries.
        Loading and unloading are charged to the matching request. Idling is
        excluded from this proportional allocation; the full schedule
        cost (including idling) remains the scheduling optimisation metric.

        Returns (total_fuel_cost, {id(trade): attributed_cost}, direct_sum).
        With `with_ballast=True`, appends the cost of empty sailing legs.
        The allocation is an AAMAS-inspired estimate, not a guarantee of
        profitability when only part of a bundle is awarded.
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
        ballast_total = 0.0
        # If a scheduled transfer is already underway, its first sailing leg
        # must not be charged again when the current stop is in progress.
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
            if not onboard:
                ballast_total += leg_cost
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
        if with_ballast:
            return total, allocation, direct_sum, ballast_total
        return total, allocation, direct_sum

    def _candidate(self, result, offered):
        """Convert a verified tentative schedule into a bid-ready route plan."""
        scheduled_ids = {id(t) for t in result.scheduled_trades}
        offered_ids = {id(t) for t in offered}
        assignments = {}
        allocation = {}
        total_cost = 0.0
        direct_sum = 0.0
        ballast_delta = 0.0
        signature = []
        for vessel in self.company.fleet:
            schedule = result.schedules.get(vessel, vessel.schedule)
            stops = self._simple_schedule(schedule)
            signature.append((id(vessel), tuple((kind, id(t)) for kind, t in stops)))
            route_total, route_alloc, route_direct, proposed_ballast = (
                self._route_allocation(vessel, schedule, with_ballast=True)
            )
            total_cost += route_total
            direct_sum += route_direct
            # Existing commitments are sunk for this auction; only *extra*
            # empty sailing caused by our candidate plan is penalised.
            key = id(vessel)
            if key not in self._baseline_ballast_cache:
                self._baseline_ballast_cache[key] = self._route_allocation(
                    vessel, vessel.schedule, with_ballast=True
                )[3]
            ballast_delta += proposed_ballast - self._baseline_ballast_cache[key]
            for kind, trade in stops:
                key = id(trade)
                if kind == "PICK_UP" and key in scheduled_ids and key in offered_ids:
                    assignments[key] = vessel
                    # Section 3.2: attribute the costs of sailing legs shared
                    # by the cargoes benefiting from those legs.
                    # Do not use result.costs[trade]: that stores an
                    # order-dependent *sequential insertion delta* there,
                    # which is not a proportional allocation of route cost.
                    allocation[key] = route_alloc.get(key, 0.0)
        coverage = len(assignments)
        efficiency = direct_sum / total_cost if total_cost > 0 else 0.0
        # A bounded fraction keeps the extra ranking term comparable across
        # small and large voyages, and avoids extreme penalties on tiny routes.
        ballast_fraction = min(1.0, max(0.0, ballast_delta) / max(1.0, total_cost))
        return _CandidatePlan(
            tuple(signature), assignments, allocation, coverage, efficiency,
            ballast_delta=ballast_delta, ballast_fraction=ballast_fraction,
        )

    def _top_plans(self, trades, deadline):
        """Build and rank candidate routes before submitting auction bids.

        Mai et al. (AAMAS 2025, Section 3.1, Algorithm 3) motivates the
        multi-start, deduplicated top-K search. Vessel-assignment sampling
        and the added ballast component in our ranking are extensions.
        """
        rng = random.Random(self.seed)
        plans = {}
        attempts = 0
        cost_fn = getattr(self.company, "estimate_schedule_cost", None)
        if not callable(cost_fn):
            cost_fn = None  # Feasibility-only fallback when no cost estimator exists.
        for run in range(self.pre_restarts):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            attempts += 1
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
                if run == 0:
                    # The first search provides a deterministic cheapest-insertion plan.
                    result = planner.plan(order)
                else:
                    # Vary vessel choices and pickup/drop-off positions as well
                    # as the trade order to avoid repeating the same plan.
                    sample_deadline = min(deadline, monotonic() + min(remaining, 1.0))
                    result = planner.sample_preauction_plan(
                        order, sample_deadline,
                        seed=self.seed + run, run_id=run,
                    )
                plan = self._candidate(result, trades)
            except Exception:
                logger.exception("Bidding: failed to evaluate candidate plan {}", run)
                continue
            old = plans.get(plan.signature)
            if old is None or self._rank_score(plan) > self._rank_score(old):
                plans[plan.signature] = plan
        # Count assignment diversity separately from route-order diversity;
        # two schedules can visit ports differently with the same vessel owner.
        assignment_patterns = {
            tuple(sorted((trade_id, id(vessel))
                         for trade_id, vessel in plan.assignments.items()))
            for plan in plans.values()
        }
        logger.info(
            "PRE_PLAN_DIVERSITY: attempts={} distinct_routes={} "
            "distinct_assignments={} top_k={}",
            attempts, len(plans), len(assignment_patterns), self.top_k,
        )
        # Retain the best route plans by consolidation quality and incremental
        # ballast. The cost blend and future positioning bonus are applied later.
        ranked = sorted(plans.values(), key=self._rank_score, reverse=True)[:self.top_k]
        if ranked:
            best = ranked[0]
            logger.info(
                "PRE_PLAN_RANK: plans={} retained={} efficiency={:.3f} "
                "extra_ballast={:.2f} ballast_fraction={:.3f} rank_score={:.3f}",
                len(plans), len(ranked), best.efficiency, best.ballast_delta,
                best.ballast_fraction, self._rank_score(best),
            )
        return ranked

    def make_bids(self, trades):
        """Choose bid-eligible trades and price them from feasible route plans.

        Based on Mai et al. (AAMAS 2025, Section 3.2, Algorithm 4):
        select trades by plan-appearance frequency and blend dedicated-trip
        cost with expected allocated cost in equal proportions. Our weighted
        plan votes, common-plan safeguard, and future bonus are extensions.
        """
        trades = list(trades)
        if not trades:
            return []
        plans = self._top_plans(trades, monotonic() + self.time_budget_s)
        if not plans:
            logger.warning("Bidding: no evaluable feasible plans; no bids")
            return []
        # Section 3.2 of Mai et al. uses request appearance across top-K
        # plans; we extend it with quality weights for those appearances.
        # A poor exploratory plan can still vote, but its vote is weaker.
        plan_weights = self._plan_weights(plans)
        total_weight = sum(plan_weights)
        weighted_presence = defaultdict(float)
        weighted_allocations = defaultdict(float)
        # Each plan contributes to both trade-selection frequency and its
        # expected allocated cost. We normalise by weight rather than count.
        for plan, weight in zip(plans, plan_weights):
            for trade_id in plan.assignments:
                weighted_presence[trade_id] += weight
                weighted_allocations[trade_id] += weight * plan.allocation[trade_id]
        probabilities = {
            id(trade): weighted_presence[id(trade)] / total_weight
            for trade in trades
        }
        if getattr(self.company, "enable_diagnostics", True):
            logger.info(
                "PRE_PLAN_WEIGHTS: plans={} score_best={:.3f} score_worst={:.3f} "
                "weight_best={:.3f} weight_worst={:.3f} effective_plans={:.2f}",
                len(plans), max(self._rank_score(p) for p in plans),
                min(self._rank_score(p) for p in plans),
                max(plan_weights), min(plan_weights),
                total_weight ** 2 / sum(w ** 2 for w in plan_weights),
            )
        # Robustness extension of AAMAS pre-scheduling: select a *single*
        # verified candidate route plan that covers the largest number of
        # frequency-eligible trades. The submitted set is then a subset of
        # that plan, rather than a union of potentially incompatible plans.
        # This does NOT guarantee that MABLE will fulfil every partial win,
        # but avoids one source of bid-set incompatibility.
        eligible_ids = {
            id(trade) for trade in trades
            if weighted_presence[id(trade)] > 0
            and probabilities[id(trade)] + 1e-12 >= self.p_select
        }
        # Choose one feasible anchor plan: bidding on the union of unrelated
        # plans could commit the same vessel to incompatible contracts.
        anchor = max(
            plans,
            key=lambda p: (
                # Primary: maximise the number of *jointly feasible* bids.
                sum(key in p.assignments for key in eligible_ids),
                # Secondary: favour the most robust trade combination.
                sum(probabilities[key] for key in eligible_ids if key in p.assignments),
                self._rank_score(p),
            ),
        )
        if getattr(self.company, "enable_diagnostics", True):
            # Compare weighted and equal-vote selection to monitor whether
            # quality weighting changes eligibility in this auction.
            uniform_counts = Counter(
                trade_id for plan in plans for trade_id in plan.assignments
            )
            uniform_ids = {
                id(trade) for trade in trades
                if uniform_counts[id(trade)] / len(plans) + 1e-12 >= self.p_select
            }
            logger.info(
                "PRE_PLAN_SELECTION: weighted_eligible={} uniform_eligible={} "
                "promoted={} suppressed={} anchor_coverage={}",
                len(eligible_ids), len(uniform_ids),
                len(eligible_ids - uniform_ids), len(uniform_ids - eligible_ids),
                sum(key in anchor.assignments for key in eligible_ids),
            )
        logger.info(
            "Bidding: {} quality-weighted eligible trades; {} jointly represented "
            "by anchor plan",
            len(eligible_ids),
            sum(key in anchor.assignments for key in eligible_ids),
        )
        bids = []
        future_discounts = 0
        for trade in trades:
            key = id(trade)
            if key not in eligible_ids or key not in anchor.assignments:
                continue
            # Absolute cost must be evaluated on the vessel from the
            # anchor plan, ensuring consistency with the shared route.
            vessel = anchor.assignments[key]
            try:
                absolute = self._dedicated_cost(vessel, trade)
                marginal = weighted_allocations[key] / weighted_presence[key]
                # Dedicated cost limits reliance on uncertain shared-route
                # savings when only a subset of the bundle wins.
                base_cost = max(MIN_BID, 0.5 * (absolute + marginal))
                if not math.isfinite(base_cost):
                    raise ValueError("Non-finite bid")
            except Exception:
                logger.exception("Bidding: could not price trade {}; skipping", trade)
                continue

            # Reduce the effective bid only when carrying this trade improves
            # the vessel's expected position for announced future cargo.
            amount = base_cost
            if getattr(self.company, "enable_future_bid_bonus", True):
                bonus_fn = getattr(self.company, "future_trade_bonus", None)
                if callable(bonus_fn):
                    try:
                        bonus = float(bonus_fn(vessel, trade, cost=base_cost))
                        if not math.isfinite(bonus) or bonus < 0:
                            raise ValueError("Invalid future-trade bonus")
                        amount = max(MIN_BID, base_cost - bonus)
                        if amount < base_cost:
                            future_discounts += 1
                    except Exception:
                        logger.exception("Bidding: Future-trade bonus failed for {}; using base bid", trade)
                        amount = base_cost

            # Save our own price and estimated cost to interpret later auction outcomes.
            record_fn = getattr(self.company, "record_our_bid", None)
            if callable(record_fn):
                try:
                    record_fn(trade, base_cost, amount)
                except Exception:
                    logger.exception("Bidding: could not record bid for {}", trade)
            bids.append(Bid(amount=amount, trade=trade))
            predicted, competitor = None, None
            try:
                predicted, competitor = self.company.estimated_competitor_bid(trade)
                record_prediction = getattr(self.company, "record_market_prediction", None)
                if callable(record_prediction):
                    record_prediction(trade, predicted, competitor)
            except Exception as exc:
                # Opponent-modelling errors may never prevent bids.
                logger.debug("Opponent price prediction unavailable: {}", exc)
            if getattr(self.company, "enable_diagnostics", True):
                try:
                    # A predicted clearing-price spread is only a diagnostic;
                    # actual contract payment depends on rival bids.
                    spread = (float(predicted) - marginal
                              if predicted is not None else None)
                    logger.info(
                        "BID_AUDIT: route={} -> {} vessel={} standalone={:.2f} "
                        "allocated={:.2f} base_bid={:.2f} future_bonus={:.2f} "
                        "bid={:.2f} predicted_market_price={} "
                        "estimated_shared_spread={} nearest_competitor={} "
                        "diagnostic_calibrated_price={}",
                        trade.origin_port, trade.destination_port,
                        getattr(vessel, "name", str(vessel)),
                        absolute, marginal, base_cost, base_cost - amount, amount,
                        "NA" if predicted is None else f"{float(predicted):.2f}",
                        "NA" if spread is None else f"{spread:.2f}",
                        competitor or "NA",
                        ("NA" if predicted is None else
                         f"{float(predicted) * self.company.market_price_calibration():.2f}"),
                    )
                except Exception as exc:
                    # Logging is optional and must not interrupt bid submission.
                    logger.debug("BID_AUDIT unavailable: {}", exc)
        logger.info(
            "Bidding: {} bids for {} trades, based on {} distinct plans (p_select={:.2f})",
            len(bids), len(trades), len(plans), self.p_select,
        )
        if hasattr(self.company, "future_trade_bonus"):
            logger.info("Future planning: future bonus adjusted {} of {} bids", future_discounts, len(bids))
        return bids

# ---------------------------------------------------------------------------
# MABLE company entry point
# ---------------------------------------------------------------------------
from mable.cargo_bidding import TradingCompany
from mable.transport_operation import ScheduleProposal
from loguru import logger
class Company6(FuturePlanningMixin, TradingCompany):
    """MABLE trading company combining bidding, routing, and future planning."""

    enable_future_bid_bonus = True
    enable_diagnostics = True  # Toggle logging only, never bid/route decisions.

    def _audit_awards(self, result, payments):
        """Print payment versus *sequential* marginal cost by awarded trade.

        Trade marginals depend on insertion order and don't each correspond to
        an independently executable vessel route. Negative is a warning to
        investigate, not proof that the award loses money in the tournament.
        """
        for trade in result.scheduled_trades:
            estimate = result.costs.get(trade)
            if estimate is None:
                continue
            paid = float(payments.get(trade, 0.0))
            owner = result.assignments.get(trade)
            logger.info(
                "TRADE_AUDIT: route={} -> {} vessel={} payment={:.2f} "
                "sequential_delta={:.2f} estimated_margin={:.2f}",
                trade.origin_port, trade.destination_port,
                getattr(owner, "name", str(owner)), paid, estimate, paid - estimate,
            )

    def _audit_schedule_activity(self, result):
        """Log predicted incremental vessel cost by activity for awarded routes.

        Per-activity deltas compare the candidate schedule with the current
        live schedule on each affected vessel. Values can be negative when
        consolidation reduces earlier costs. They are NOT actual spent fuel.
        """
        categories = ("ballast", "laden", "loading", "unloading", "idle")
        hours = ("ballast_hours", "laden_hours", "idle_hours")
        totals = dict.fromkeys((*categories, *hours), 0.0)
        for vessel, updated_schedule in result.schedules.items():
            try:
                before = estimate_schedule_cost(
                    vessel, vessel.schedule, headquarters=self.headquarters,
                    return_breakdown=True,
                )
                after = estimate_schedule_cost(
                    vessel, updated_schedule, headquarters=self.headquarters,
                    return_breakdown=True,
                )
                delta = {key: after[key] - before[key] for key in totals}
                for key, value in delta.items():
                    totals[key] += value
                logger.info(
                    "FUEL_AUDIT: vessel={} ballast={:.2f} laden={:.2f} "
                    "loading={:.2f} unloading={:.2f} idle={:.2f} "
                    "ballast_hours={:.2f} laden_hours={:.2f} "
                    "idle_hours={:.2f} estimated_added_cost={:.2f}",
                    getattr(vessel, "name", str(vessel)),
                    *(delta[key] for key in categories),
                    *(delta[key] for key in hours),
                    after["total"] - before["total"],
                )
            except Exception as exc:
                # No diagnostic exception may block schedule submission.
                logger.warning("FUEL_AUDIT unavailable for vessel {}: {}",
                               getattr(vessel, "name", str(vessel)), exc)
        logger.info(
            "FUEL_AUDIT_TOTAL: ballast={:.2f} laden={:.2f} "
            "loading={:.2f} unloading={:.2f} idle={:.2f} "
            "ballast_hours={:.2f} laden_hours={:.2f} idle_hours={:.2f} "
            "estimated_added_cost={:.2f} scheduler_estimate={:.2f}",
            *(totals[key] for key in categories),
            *(totals[key] for key in hours),
            sum(totals[key] for key in categories), result.total_extra_cost,
        )

    def inform(self, trades, *args, **kwargs):
        """Generate auction bids from feasible pre-auction route plans."""
        try:
            self._model_init()
            self._model_price_predictions = {}  # fresh pre-auction forecast snapshot
            # Snapshot public competitor locations for future-trade valuation.
            try:
                self.snapshot_competitors()
                logger.info(
                    "Future planning: tracking {} future trades and {} competitors",
                    len(self._future_trades), len(self._model_comp_snapshot)
                )
            except Exception:
                logger.exception("Future planning: Could not snapshot competitors; bidding continues")
            return BiddingStrategy(self).make_bids(trades)
        except Exception:
            logger.exception("Error in Company6.inform()")
            return []

    def receive(self, contracts, auction_ledger=None, *args, **kwargs):
        """Schedule won contracts while preserving existing vessel commitments."""
        # Update diagnostic price estimates independently of contract scheduling.
        try:
            self.update_opponent_models(auction_ledger)
        except Exception:
            logger.exception("Future planning: Could not update opponent model; scheduling continues")
        trades = [contract.trade for contract in contracts]
        if trades:
            # Following Mai et al. (AAMAS 2025, Section 3.3), plan against
            # awarded payments and incremental cost, with an additional
            # estimate of non-delivery penalty exposure.
            payments = {contract.trade: contract.payment for contract in contracts}
            result = self.make_scheduler(time_budget_s=12.0).plan(
                trades, payments=payments
            )

            assigned = set(result.scheduled_trades)
            missing = [trade for trade in trades if trade not in assigned]
            if missing:
                logger.warning(
                    "{} of {} awarded trades could not be planned: {}",
                    len(missing), len(trades), missing,
                )

            if result.schedules:
                logger.info(
                    "POST_AUCTION: awarded={} scheduled={} estimated_added_fleet_cost={:.2f}",
                    len(trades), len(result.scheduled_trades), result.total_extra_cost,
                )
                if self.enable_diagnostics:
                    try:
                        self._audit_awards(result, payments)
                        self._audit_schedule_activity(result)
                    except Exception:
                        logger.exception("Cost diagnostics failed; schedule submission continues")
                rejected = self.apply_schedules(result.schedules)
                if rejected:
                    logger.warning("MABLE rejected schedule(s): {}", rejected)

    # Supply the company's network context to the cost estimator.
    # Both the scheduler and the bidder call these methods.
    def estimate_schedule_cost(self, vessel, schedule):
        return estimate_schedule_cost(
            vessel, schedule, headquarters=self.headquarters
        )

    def estimate_trade_cost(self, vessel, trade):
        return estimate_trade_cost(
            vessel, trade, headquarters=self.headquarters
        )

    def estimate_non_delivery_penalty(self, trade):
        """Conservative proxy for MABLE's non-delivery penalty.

        Use a dedicated voyage fuel cost as a conservative penalty proxy
        for the largest capable vessel (ballast to origin, laden delivery,
        loading and unloading). MABLE may compute the real penalty from the
        vessel's *end-of-simulation* position, which we cannot know here.
        Actual simulator penalties may differ from this cost proxy.
        """
        candidates = []
        for vessel in self.fleet:
            capacity = None
            for attribute in ("get_max_capacity", "capacity"):
                method = getattr(vessel, attribute, None)
                if callable(method):
                    try:
                        capacity = float(method(trade.cargo_type))
                        break
                    except (TypeError, ValueError, AttributeError):
                        pass
            if capacity is not None and math.isfinite(capacity):
                candidates.append((capacity, vessel))
        if not candidates:
            raise CostEstimationError("Could not estimate penalty: vessel capacities unavailable")
        capacity, vessel = max(candidates, key=lambda item: item[0])
        # Use the largest capable vessel as a conservative reference for the
        # cost of an undelivered contract.
        end_fn = getattr(self, "_model_end_location", None)
        start = end_fn(vessel) if callable(end_fn) else vessel.location
        origin = trade.origin_port
        destination = trade.destination_port
        ballast_dist = _get_distance(start, origin, vessel, self.headquarters)
        laden_dist = _get_distance(origin, destination, vessel, self.headquarters)
        loading_time = vessel.get_loading_time(trade.cargo_type, trade.amount)
        ballast_time = vessel.get_travel_time(ballast_dist)
        laden_time = vessel.get_travel_time(laden_dist)
        fuel = (
            vessel.get_ballast_consumption(ballast_time, vessel.speed)
            + vessel.get_laden_consumption(laden_time, vessel.speed)
            + 2.0 * vessel.get_loading_consumption(loading_time)
        )
        return _fuel_cost(vessel, fuel)

    def make_scheduler(self, time_budget_s=8.0):
        return SchedulingOptimizer(
            fleet=self.fleet,
            cost_fn=self.estimate_schedule_cost,
            penalty_fn=self.estimate_non_delivery_penalty,
            restarts=8,
            time_budget_s=time_budget_s,
            post_rcl_size=3,
            local_search_passes=2,
            local_search_max_moves=8,
        )

    def propose_schedules(self, trades):
        """Build a feasible tentative schedule for offered trades."""
        result = self.make_scheduler(time_budget_s=8.0).plan(trades)
        return ScheduleProposal(
            result.schedules, result.scheduled_trades, result.costs
        )
