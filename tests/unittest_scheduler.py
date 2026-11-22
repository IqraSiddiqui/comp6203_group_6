"""Standalone Person 2 tests -- no MABLE installation required.
Run: python -m unittest -v test_person2_complete.py
"""
import unittest
from dataclasses import dataclass
from source.scheduler import SchedulingOptimizer


@dataclass(frozen=True)
class FakeTrade:
    name: str
    pickup: int
    dropoff: int
    amount: int = 1
    latest_pickup: int = 100


class FakeSchedule:
    def __init__(self, vessel, steps=()):
        self.vessel = vessel
        self.steps = list(steps)

    def copy(self):
        return FakeSchedule(self.vessel, self.steps)

    def get_insertion_points(self):
        return list(range(1, len(self.steps) + 2))

    def add_transportation(self, trade, location_pick_up=None, location_drop_off=None):
        pickup = location_pick_up if location_pick_up is not None else len(self.steps) + 1
        dropoff = location_drop_off if location_drop_off is not None else pickup
        if not (1 <= pickup <= len(self.steps) + 1 and pickup <= dropoff <= len(self.steps) + 1):
            raise ValueError("Invalid insertion points")
        self.steps.insert(pickup - 1, ("pickup", trade))
        self.steps.insert(dropoff, ("dropoff", trade))

    def verify_schedule(self):
        onboard = 0
        picked = set()
        for action, trade in self.steps:
            if action == "pickup":
                if trade in picked:
                    return False
                picked.add(trade)
                onboard += trade.amount
            else:
                if trade not in picked:
                    return False
                onboard -= trade.amount
            if onboard < 0 or onboard > self.vessel.capacity:
                return False
        return onboard == 0


class FakeVessel:
    def __init__(self, name, start, capacity=2):
        self.name = name
        self.start = start
        self.capacity = capacity
        self.schedule = FakeSchedule(self)


def mock_total_schedule_cost(vessel, schedule):
    """Mock route-distance cost; NOT real fuel cost. Person 1 will replace."""
    current = vessel.start
    cost = 0.0
    for action, trade in schedule.steps:
        location = trade.pickup if action == "pickup" else trade.dropoff
        cost += abs(location - current)
        current = location
    return cost


class SchedulerTests(unittest.TestCase):
    def test_cheapest_vessel(self):
        far, near = FakeVessel('far', 0), FakeVessel('near', 20)
        trade = FakeTrade('T', 21, 30)
        result = SchedulingOptimizer([far, near], mock_total_schedule_cost, restarts=3).plan([trade])
        self.assertIn(near, result.schedules)
        self.assertNotIn(far, result.schedules)
        self.assertEqual(result.costs[trade], 10)

    def test_insertion_beats_append_without_changing_live_schedule(self):
        vessel = FakeVessel('ship', 0, capacity=2)
        existing = FakeTrade('A', 10, 30)
        new = FakeTrade('B', 11, 31)
        vessel.schedule.add_transportation(existing)
        result = SchedulingOptimizer([vessel], mock_total_schedule_cost, restarts=4).plan([new])
        actions = [(action, trade.name) for action, trade in result.schedules[vessel].steps]
        self.assertEqual(actions, [
            ('pickup', 'A'), ('pickup', 'B'), ('dropoff', 'A'), ('dropoff', 'B')
        ])
        self.assertEqual(len(vessel.schedule.steps), 2)
        self.assertEqual(result.costs[new], 1)

    def test_capacity_cannot_be_violated(self):
        vessel = FakeVessel('ship', 0, capacity=1)
        trades = [FakeTrade('A', 10, 30), FakeTrade('B', 11, 31)]
        result = SchedulingOptimizer([vessel], mock_total_schedule_cost).plan(trades)
        self.assertEqual(len(result.scheduled_trades), 2)
        self.assertTrue(result.schedules[vessel].verify_schedule())

    def test_oversize_trade_not_scheduled(self):
        vessel = FakeVessel('ship', 0, capacity=1)
        trade = FakeTrade('too-large', 5, 10, amount=2)
        result = SchedulingOptimizer([vessel], mock_total_schedule_cost).plan([trade])
        self.assertEqual(result.scheduled_trades, [])
        self.assertEqual(result.schedules, {})

    def test_can_run_before_cost_model_is_ready(self):
        vessel = FakeVessel('ship', 0)
        trade = FakeTrade('T', 5, 10)
        result = SchedulingOptimizer([vessel], cost_fn=None).plan([trade])
        self.assertEqual(result.scheduled_trades, [trade])
        self.assertEqual(result.costs, {})  # no invented monetary costs

    def test_multi_start_gives_feasible_plan(self):
        vessel = FakeVessel('ship', 0)
        trades = [FakeTrade('A', 10, 30), FakeTrade('B', 11, 31), FakeTrade('C', 12, 32)]
        result = SchedulingOptimizer([vessel], mock_total_schedule_cost, restarts=10).plan(trades)
        self.assertEqual(set(result.scheduled_trades), set(trades))
        self.assertTrue(result.schedules[vessel].verify_schedule())

    def test_payment_is_tiebreaker_when_some_trades_cannot_be_taken(self):
        # A capacity-1 vessel can take either trade, but each trade is time-
        # window incompatible with the other in this fake by special schedule.
        class OneTradeOnly(FakeSchedule):
            def copy(self):
                return OneTradeOnly(self.vessel, self.steps)
            def verify_schedule(self):
                return (super().verify_schedule() and
                        sum(a == 'pickup' for a, _ in self.steps) <= 1)
        vessel = FakeVessel('ship', 0)
        vessel.schedule = OneTradeOnly(vessel)
        cheap, valuable = FakeTrade('cheap', 10, 20), FakeTrade('valuable', 20, 30)
        result = SchedulingOptimizer([vessel], mock_total_schedule_cost, restarts=6).plan(
            [cheap, valuable], payments={cheap: 10, valuable: 1000})
        self.assertEqual(result.scheduled_trades, [valuable])

    def test_empty(self):
        result = SchedulingOptimizer([], cost_fn=None).plan([])
        self.assertEqual(result.scheduled_trades, [])
        self.assertEqual(result.schedules, {})


if __name__ == '__main__':
    unittest.main()
