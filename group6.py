"""COMP6203 Maritime Shipping Trading agent — Group 6.

Required MABLE entry point: class Company6(FuturePlanningMixin, TradingCompany).
This file is a self-contained deployment version of the four-person team code:
 Person 1: cost model; Person 2: scheduler; Person 3: AAMAS-inspired bidding;
 Person 4: future planning and opponent modelling.

This is the single-file coursework submission. No project-local source/ imports
are required. Edit this file directly when changing the team agent.
"""


# ================== BEGIN cost_model.py ==================
"""
MABLE Cost Estimator
--------------------

Person 1: Cost Model

This module provides three reusable functions:

1. estimate_trade_cost()
   Estimates the standalone cost of transporting one trade.

2. estimate_schedule_cost()
   Estimates the total cost of carrying out a vessel's complete schedule.

3. estimate_marginal_cost()
   Estimates the extra cost of adding a trade to an existing schedule.

The calculations use MABLE's vessel, schedule and fuel methods.

Cost components:
- Loading fuel
- Unloading fuel
- Laden travel fuel
- Ballast travel fuel
- Idle fuel while waiting

The functions return costs in the monetary units used by MABLE.
"""

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
    headquarters=None
) -> float:
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

    if not events:
        return 0.0

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

    return _check_number(
        total_cost,
        "Total schedule cost"
    )


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

    marginal_cost = updated_cost - original_cost

    if not math.isfinite(marginal_cost):
        raise CostEstimationError(
            "Marginal cost calculation produced an invalid result."
        )

    return marginal_cost
# ================== END cost_model.py ==================


# ================== BEGIN scheduler.py ==================
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
    """Multi-start feasible insertion, scored using estimated *net income*.

    Pre-auction uses coverage then cost.  Post-auction payments are already
    awarded, so the income comparison between schedules is equivalent to
    minimising incremental fleet operating cost PLUS estimated penalties for
    contracts left unfulfilled. If penalty estimates cannot be obtained,
    revert to fulfilment-first ranking; never manufacture a penalty value.

    For post-auction search, the first restart is deterministic greed; later
    restarts sample from a restricted candidate list (AAMAS-inspired GRASP).
    Every trial schedule is copied and verified by MABLE.
    """

    def __init__(self, fleet, cost_fn=None, restarts=8,
                 time_budget_s=8.0, seed=42, penalty_fn=None,
                 post_rcl_size=3):
        self.fleet = list(fleet)
        if cost_fn is not None and not callable(cost_fn):
            raise TypeError("cost_fn must be callable or None")
        if penalty_fn is not None and not callable(penalty_fn):
            raise TypeError("penalty_fn must be callable or None")
        self.cost_fn = cost_fn
        self.penalty_fn = penalty_fn
        self.post_rcl_size = max(1, int(post_rcl_size))
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

    def _post_auction_run(self, order, payments, deadline,
                          penalties=None, rng=None, rcl_size=1):
        """Construct a revenue/cost-aware post-award schedule.

        With penalty estimates, priority is penalty avoided minus insertion
        cost. This reflects that awarded payments are already fixed across
        all schedules. Without estimates, use AAMAS payment minus cost.

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

    def plan(self, trades, payments=None):
        """Return the best verified plan found within the time budget.

        `payments` must contain *awarded* contract payments, not bid guesses.
        Existing vessel schedule costs are subtracted via marginal costing.
        """
        trades = list(trades)
        if not trades:
            return PlanningResult()
        deadline = monotonic() + self.time_budget_s
        rng = Random(self.seed)
        penalties = self._penalty_estimates(trades) if payments is not None else None

        # Preserve a quick feasible fallback (no penalty-driven omissions).
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

        for run, order in enumerate(orders[:self.restarts]):
            if monotonic() >= deadline:
                break
            if payments is None:
                candidate = self._one_run(order, deadline, append_only=False)
            else:
                candidate = self._post_auction_run(
                    order, payments, deadline, penalties=penalties,
                    rng=rng, rcl_size=(1 if run == 0 else self.post_rcl_size),
                )
            if self._quality(candidate, payments, penalties, trades) > \
                    self._quality(best, payments, penalties, trades):
                best = candidate

        # Last chance: insert any unserved awarded trades without disturbing
        # commitments, and never worsen a feasible full-coverage result.
        assigned = set(best.scheduled_trades)
        for trade in trades:
            if monotonic() >= deadline:
                break
            if trade in assigned:
                continue
            choice = self.best_insertion(trade, best.schedules, deadline)
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
        return best

# ================== END scheduler.py ==================


# ================== BEGIN future_planning.py ==================
"""
Person 4 - Future Planning + Opponent Modelling   (COMP6203, Lab 3 Ex 1-3)
===========================================================================

Embedded in group6.py above Company6; it is inherited first:

    class Company6(FuturePlanningMixin, TradingCompany):
        ...

What it gives the rest of the team
----------------------------------
For Person 3 (bidding / inform):
    self.snapshot_competitors()                    call once at the start of inform()
    self.future_trade_bonus(vessel, trade, cost, schedule=None)
          -> £ amount the trade is worth EXTRA because of where it leaves the
             vessel for next month's cargoes. Subtract it from the cost before bidding.
    self.estimated_competitor_bid(trade)
          -> (predicted lowest competitor bid, company name) or (None, None)
    self.expected_profit(trade, our_cost)
          -> predicted payment - our cost. Useful for ranking trades when a
             vessel can only take some of them.
    self.record_our_bid(trade, cost, bid)          call for every bid you submit

For Person 2 (receive):
    self.update_opponent_models(auction_ledger)    call at the start of receive()

Report / analysis:
    self.opponent_summary()                        dict with the learned factors

Auction note for the team
-------------------------
The market uses a SECOND-price reverse auction. If we win, we are paid the
second-lowest bid, not our own. So:
  * Raising our bid does not raise what we are paid. It only raises the
    chance of losing. Bidding close to our true (effective) cost is the
    baseline strategy.
  * The `payment` shown in auction_ledger is the RUNNER-UP's bid, not the
    winner's. The factors learned below therefore describe "the price the
    market clears at". That is exactly what we need to predict what we
    would be paid.
Our "true cost" for a trade = marginal schedule cost (P1/P2) minus
future_trade_bonus (P4). That is where P4 plugs into the bid.

Only public information is used: competitor fleets, their vessels' current
locations and capacities (as in Lab 3 Ex 2), and the auction ledger (Lab 3 Ex 3).
Nothing reads competitors' schedules or bids.
"""

import statistics
import math


class FuturePlanningMixin:

    # ---- tuning knobs (good things to vary in the report's experiments) ----
    FUTURE_WEIGHT = 0.6         # how much of the estimated saving we trust
    FUTURE_CAP_FRACTION = 0.30  # bonus never exceeds 30% of the trade's cost
    FUTURE_TOP_K = 2            # count the best K follow-on trades (decaying)
    FUTURE_DECAY = 0.5          # weight of the 2nd-best, 3rd-best, ... trade
    DEFAULT_MARKET_FACTOR = 1.2 # assumed competitor markup before we have data
    MIN_SAMPLES = 3             # ledger samples needed before trusting a factor
    FACTOR_CLIP = (0.5, 5.0)    # discard silly ratios (bad cost guesses)

    # ------------------------------------------------------------------ state
    def _p4_init(self):
        if getattr(self, "_p4_ready", False):
            return
        self._p4_ready = True
        self._future_trades = []          # trades of NEXT month's auction
        self._future_auction_time = None
        self._p4_dist_cache = {}
        self._p4_comp_snapshot = {}       # company name -> [(vessel, location)]
        self._p4_ratios = {}              # company name -> [payment / est. cost]
        self._p4_market_ratios = []       # all companies pooled
        self._p4_our_bids = {}            # trade key -> (our cost, our bid)
        self._p4_lost = []                # (payment / our cost) on trades we lost
        self._p4_won = []                 # (payment / our cost) on trades we won

    # ======================================================== pre_inform
    def pre_inform(self, trades, time):
        """Called by the market before inform(): `trades` will be auctioned at `time`."""
        self._p4_init()
        self._future_trades = list(trades) if trades else []
        self._future_auction_time = time

    # ============================================================ helpers
    @staticmethod
    def _p4_key(trade):
        """Stable key for a trade, so dicts don't depend on object identity."""
        return (str(trade.origin_port), str(trade.destination_port),
                round(float(trade.amount), 3),
                getattr(trade, "earliest_pickup", None),
                getattr(trade, "latest_drop_off", None))

    def _p4_distance(self, a, b):
        key = (str(a), str(b))
        if key not in self._p4_dist_cache:
            try:
                d = float(self.headquarters.get_network_distance(a, b))
                if not math.isfinite(d) or d < 0:
                    d = float("inf")
            except (TypeError, ValueError, AttributeError):
                d = float("inf")   # unreachable / unknown pair
            self._p4_dist_cache[key] = d
        return self._p4_dist_cache[key]

    @staticmethod
    def _p4_ballast_cost(vessel, distance):
        """Fuel cost of sailing `distance` NM empty."""
        if distance == float("inf"):
            return float("inf")
        if distance <= 0:
            return 0.0
        t = vessel.get_travel_time(distance)
        return vessel.get_cost(vessel.get_ballast_consumption(t, vessel.speed))

    def _p4_standalone_cost(self, vessel, trade, start_location=None):
        """Lab 3 Ex 3 style cost: reach origin + load + laden trip + unload.
        Used to cost a COMPETITOR's vessel. P1's functions cost our own vessels."""
        start = start_location if start_location is not None else vessel.location
        ballast = self._p4_ballast_cost(vessel, self._p4_distance(start, trade.origin_port))
        load_t = vessel.get_loading_time(trade.cargo_type, trade.amount)
        load = vessel.get_cost(vessel.get_loading_consumption(load_t))
        unload = vessel.get_cost(vessel.get_unloading_consumption(load_t))
        laden_d = self._p4_distance(trade.origin_port, trade.destination_port)
        laden_t = vessel.get_travel_time(laden_d)
        laden = vessel.get_cost(vessel.get_laden_consumption(laden_t, vessel.speed))
        return ballast + load + laden + unload

    def _p4_end_location(self, vessel):
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
    def _p4_can_carry(vessel, trade):
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
        self._p4_init()
        best, best_d = None, float("inf")
        for f in self._future_trades:
            d = self._p4_distance(trade.destination_port, f.origin_port)
            if d < best_d:
                best, best_d = f, d
        return best, best_d

    # ========================= Lab 3 Ex 2: competitors' closest vessels
    def snapshot_competitors(self):
        """Record competitor vessel positions BEFORE the auction. Call in inform().
        receive() runs after vessels may have moved, so we cost from this snapshot."""
        self._p4_init()
        snap = {}
        for company in self.headquarters.get_companies():
            if company.name == self.name:
                continue
            snap[company.name] = [(v, v.location) for v in company.fleet]
        self._p4_comp_snapshot = snap

    def find_closest_competitor_vessels(self, trade):
        """{company name: (vessel, distance NM to trade origin)} for every competitor."""
        self._p4_init()
        if not self._p4_comp_snapshot:
            self.snapshot_competitors()
        result = {}
        for name, vessels in self._p4_comp_snapshot.items():
            best = None
            for v, loc in vessels:
                if not self._p4_can_carry(v, trade):
                    continue
                d = self._p4_distance(loc, trade.origin_port)
                if best is None or d < best[1]:
                    best = (v, d)
            if best is not None:
                result[name] = best
        return result

    def competitor_costs(self, trade):
        """{company name: estimated cheapest standalone cost for that company}."""
        self._p4_init()
        if not self._p4_comp_snapshot:
            self.snapshot_competitors()
        out = {}
        for name, vessels in self._p4_comp_snapshot.items():
            costs = [self._p4_standalone_cost(v, trade, loc)
                     for v, loc in vessels if self._p4_can_carry(v, trade)]
            costs = [c for c in costs if c != float("inf")]
            if costs:
                out[name] = min(costs)
        return out

    # ================================================ future-opportunity score
    def future_trade_bonus(self, vessel, trade, cost=None, schedule=None):
        """£ value of `vessel` ending at trade.destination_port, because of next
        month's cargoes.

        For each future trade f:
          baseline = cheapest ballast from ANY of our vessels' current end points to f
                     (if another ship of ours is already close, repositioning this
                     one adds little)
          new      = ballast from trade.destination_port to f, by this vessel
          saving   = max(0, baseline - new)
          p_win    = rough chance we win f, from how close the nearest competitor is
        Bonus = FUTURE_WEIGHT * sum of the top-K (p_win * saving), with decay, capped
        at FUTURE_CAP_FRACTION * cost.
        """
        self._p4_init()
        if not self._future_trades:
            return 0.0  # last month, or pre_inform not called yet

        end_time = None
        if schedule is not None:
            try:
                end_time = schedule.completion_time()
            except Exception:
                end_time = None

        values = []
        for f in self._future_trades:
            if not self._p4_can_carry(vessel, f):
                continue
            new_d = self._p4_distance(trade.destination_port, f.origin_port)
            if new_d == float("inf"):
                continue

            # Rough time check: can we reach f's pickup in time?
            if end_time is not None and f.latest_pickup is not None:
                if end_time + vessel.get_travel_time(new_d) > f.latest_pickup:
                    continue

            base_d = min(self._p4_distance(self._p4_end_location(v), f.origin_port)
                         for v in self.fleet)
            if base_d == float("inf"):
                base_d = new_d * 2  # nothing of ours can currently reach it
            saving = (self._p4_ballast_cost(vessel, base_d)
                      - self._p4_ballast_cost(vessel, new_d))
            if saving <= 0:
                continue

            comp = self.find_closest_competitor_vessels(f)
            comp_d = min((d for _, d in comp.values()), default=float("inf"))
            if comp_d == float("inf"):
                p_win = 0.9
            else:
                p_win = comp_d / (comp_d + new_d + 1e-9)  # closer than them -> >0.5
                p_win = min(0.9, max(0.1, p_win))
            values.append(p_win * saving)

        if not values:
            return 0.0
        values.sort(reverse=True)
        total = sum(v * (self.FUTURE_DECAY ** i)
                    for i, v in enumerate(values[: self.FUTURE_TOP_K]))
        bonus = self.FUTURE_WEIGHT * total
        if cost is not None and cost > 0:
            bonus = min(bonus, self.FUTURE_CAP_FRACTION * cost)
        return float(bonus)

    # =================================== Lab 3 Ex 3: auction-outcome analysis
    def record_our_bid(self, trade, cost, bid):
        """Person 3: call for every bid submitted, so we can learn from outcomes."""
        self._p4_init()
        self._p4_our_bids[self._p4_key(trade)] = (cost, bid)

    def update_opponent_models(self, auction_ledger):
        """Person 2: call at the start of receive(contracts, auction_ledger=None, ...).

        auction_ledger = {company name: [Contract(trade, payment), ...]}
        """
        self._p4_init()
        if not auction_ledger:
            return
        for name, contracts in auction_ledger.items():
            for c in contracts or []:
                trade, payment = c.trade, c.payment
                key = self._p4_key(trade)
                ours = self._p4_our_bids.get(key)

                if name == self.name:
                    if ours and ours[0] > 0:
                        self._p4_won.append(payment / ours[0])
                    continue

                # A competitor won. Estimate their cost from the pre-auction snapshot.
                vessels = self._p4_comp_snapshot.get(name)
                if vessels:
                    costs = [self._p4_standalone_cost(v, trade, loc)
                             for v, loc in vessels if self._p4_can_carry(v, trade)]
                    costs = [x for x in costs if 0 < x < float("inf")]
                    if costs:
                        r = payment / min(costs)
                        if self.FACTOR_CLIP[0] <= r <= self.FACTOR_CLIP[1]:
                            self._p4_ratios.setdefault(name, []).append(r)
                            self._p4_market_ratios.append(r)

                if ours and ours[0] > 0:
                    self._p4_lost.append(payment / ours[0])
        self._p4_our_bids = {}

    def competitor_factor(self, name):
        """Learned payment/cost factor for one competitor (median, robust to outliers)."""
        self._p4_init()
        r = self._p4_ratios.get(name, [])
        if len(r) >= self.MIN_SAMPLES:
            return statistics.median(r)
        return self.market_factor()

    def market_factor(self):
        self._p4_init()
        if len(self._p4_market_ratios) >= self.MIN_SAMPLES:
            return statistics.median(self._p4_market_ratios)
        return self.DEFAULT_MARKET_FACTOR

    def estimated_competitor_bid(self, trade):
        """(lowest predicted competitor bid, company name). This approximates what we
        would be paid if we win, because the auction is second-price."""
        best, who = None, None
        for name, c in self.competitor_costs(trade).items():
            b = c * self.competitor_factor(name)
            if best is None or b < best:
                best, who = b, name
        return best, who

    def expected_profit(self, trade, our_cost):
        """Predicted payment minus our cost (None if no competitor can carry it)."""
        bid, _ = self.estimated_competitor_bid(trade)
        return None if bid is None else bid - our_cost

    def lost_margin_hint(self):
        """Median of (payment / our cost) on trades we LOST. Below ~1.0 means
        competitors clear below our cost: our cost model or markup is too high."""
        self._p4_init()
        return statistics.median(self._p4_lost) if self._p4_lost else None

    def opponent_summary(self):
        """Learned figures for logging and for the report."""
        self._p4_init()
        return {
            "per_company_factor": {n: round(statistics.median(r), 3)
                                   for n, r in self._p4_ratios.items() if r},
            "samples": {n: len(r) for n, r in self._p4_ratios.items()},
            "market_factor": round(self.market_factor(), 3),
            "won_payment_over_cost": (round(statistics.median(self._p4_won), 3)
                                      if self._p4_won else None),
            "lost_payment_over_cost": (round(self.lost_margin_hint(), 3)
                                       if self._p4_lost else None),
        }

# ================== END future_planning.py ==================


# ================== BEGIN bidding.py ==================
"""Person 3: standalone bidding strategy for the MABLE shipping agent.

This is an ADAPTATION of Person 3's Phase 1/2 strategy to Person 2's
SchedulingOptimizer. It does not import Person 3's alternative Company6,
_VesselModel, _Plan, or post-auction scheduling implementation.

Pre-auction: sample distinct feasible plans; rank by consolidation
efficiency and retain the best K; estimate appearance probability p_r,
then bid 0.5 * (dedicated_cost + mean allocated route cost) for requests
with p_r >= P_SELECT. The route allocation follows the leg-sharing
heuristic in Mai et al. (AAMAS 2025, Section 3.2). Person 1's full
schedule-cost function is used separately for insertion decisions.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import random
from time import monotonic

from loguru import logger
from mable.transport_operation import Bid

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
        """Person 1's standalone cost (or existing estimate if unavailable)."""
        person1 = getattr(self.company, "estimate_trade_cost", None)
        if callable(person1):
            cost = float(person1(vessel, trade))
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid Person 1 standalone trade cost")
            return cost
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
        excluded from this proportional allocation; Person 1's full schedule
        cost (including idling) remains the scheduling optimisation metric.

        Returns (total_fuel_cost, {id(trade): attributed_cost}, direct_sum).
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
                    # AAMAS Sec. 3.2: use the cost of route legs shared
                    # between the cargoes benefiting from those legs.
                    # Do NOT use result.costs[trade]: Person 2 stores an
                    # order-dependent *sequential insertion delta* there,
                    # which is not a proportional allocation of route cost.
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
            if old is None or plan.efficiency > old.efficiency:
                plans[plan.signature] = plan
        # AAMAS 2025 Algorithm 3: rank by consolidation efficiency,
        # rather than ranking first by the number of trades covered.
        # Each candidate remains individually verified by Person 2's
        # SchedulingOptimizer; this does not guarantee joint auction wins.
        return sorted(plans.values(), key=lambda p: p.efficiency, reverse=True)[:self.top_k]

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
        # Robustness extension of AAMAS pre-scheduling: select a *single*
        # verified candidate route plan that covers the largest number of
        # frequency-eligible trades. The submitted set is then a subset of
        # that plan, rather than a union of potentially incompatible plans.
        # This does NOT guarantee that MABLE will fulfil every partial win,
        # but avoids one source of bid-set incompatibility.
        eligible_ids = {
            id(trade) for trade in trades
            if frequencies[id(trade)]
            and frequencies[id(trade)] / len(plans) >= self.p_select
        }
        anchor = max(
            plans,
            key=lambda p: (
                sum(key in p.assignments for key in eligible_ids),
                p.efficiency,
            ),
        )
        logger.info(
            "Bidding: {} frequency-eligible trades; {} jointly represented "
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
                marginal = allocated_sums[key] / frequencies[key]
                # Person 3's base valuation, prior to future-opportunity adjustment.
                base_cost = max(MIN_BID, 0.5 * (absolute + marginal))
                if not math.isfinite(base_cost):
                    raise ValueError("Non-finite bid")
            except Exception:
                logger.exception("Bidding: could not price trade {}; skipping", trade)
                continue

            # Person 4: discounted estimated future repositioning opportunity.
            # This can be switched off for an A/B comparison by setting
            # `company.enable_future_bid_bonus = False`.
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
                        logger.exception("Bidding: P4 bonus failed for {}; using base bid", trade)
                        amount = base_cost

            # Person 4: remember our own cost estimate and bid for ledger analysis.
            record_fn = getattr(self.company, "record_our_bid", None)
            if callable(record_fn):
                try:
                    record_fn(trade, base_cost, amount)
                except Exception:
                    logger.exception("Bidding: could not record bid for {}", trade)
            bids.append(Bid(amount=amount, trade=trade))
        logger.info(
            "Bidding: {} bids for {} trades, based on {} distinct plans (p_select={:.2f})",
            len(bids), len(trades), len(plans), self.p_select,
        )
        if hasattr(self.company, "future_trade_bonus"):
            logger.info("P4: future bonus adjusted {} of {} bids", future_discounts, len(bids))
        return bids

# ================== END bidding.py ==================


# ================== AGENT ENTRY POINT ==================
from mable.cargo_bidding import TradingCompany
from mable.transport_operation import ScheduleProposal
from loguru import logger
class Company6(FuturePlanningMixin, TradingCompany):
    """Person 4 provides pre_inform() through FuturePlanningMixin."""

    def inform(self, trades, *args, **kwargs):
        """Person 3's bidding strategy, using Person 2's feasible plans."""
        try:
            # Person 4: record publicly observable competitor positions before auction.
            try:
                self.snapshot_competitors()
                logger.info(
                    "P4: tracking {} future trades and {} competitors",
                    len(self._future_trades), len(self._p4_comp_snapshot)
                )
            except Exception:
                logger.exception("P4: Could not snapshot competitors; bidding continues")
            return BiddingStrategy(self).make_bids(trades)
        except Exception:
            logger.exception("Error in Company6.inform()")
            return []

    def receive(self, contracts, auction_ledger=None, *args, **kwargs):
        """schedule actual awarded trades, preserving commitments"""
        # Person 4: learn from the auction ledger, without affecting delivery.
        try:
            self.update_opponent_models(auction_ledger)
        except Exception:
            logger.exception("P4: Could not update opponent model; scheduling continues")
        trades = [contract.trade for contract in contracts]
        if trades:
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
                rejected = self.apply_schedules(result.schedules)
                if rejected:
                    logger.warning("MABLE rejected schedule(s): {}", rejected)

    # Person 1: bind the company's routing context to the standalone cost model.
    # Person 2's scheduler and Person 3's bidder both discover this method.
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

        Based on Person 3's original penalty estimator: dedicated voyage fuel
        for the largest capable vessel (ballast to origin, laden delivery,
        loading and unloading). MABLE may compute the real penalty from the
        vessel's *end-of-simulation* position, which we cannot know here.
        This is an estimate, not a guaranteed reproduction of MABLE's rule.
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
        # MABLE's penalty estimate is based on the largest vessel, not
        # necessarily one that would be selected by the route scheduler.
        end_fn = getattr(self, "_p4_end_location", None)
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
        )

    def propose_schedules(self, trades):
        """pre-auction minimum-cost feasible planning"""
        result = self.make_scheduler(time_budget_s=8.0).plan(trades)
        return ScheduleProposal(
            result.schedules, result.scheduled_trades, result.costs
        )
