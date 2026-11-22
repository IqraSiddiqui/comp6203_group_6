#person 1 add ur code here
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