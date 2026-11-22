"""Mocked smoke and numerical tests for Person 1 + Persons 2/3/4 integration.

From the project root:
    python -m unittest discover -s tests -p "unittest_cost_integration.py" -v

These tests do not replace an end-to-end run with the real MABLE package.
"""
from dataclasses import dataclass
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


@dataclass(frozen=True)
class Port:
    name: str
    x: float


@dataclass(frozen=True)
class Trade:
    name: str
    origin_port: Port
    destination_port: Port
    amount: float = 10.0
    cargo_type: str = "Oil"
    earliest_pickup: float = 0.0
    latest_drop_off: float = 2000.0


@dataclass
class Bid:
    amount: float
    trade: object


@dataclass
class Proposal:
    schedules: dict
    scheduled_trades: list
    costs: dict


class TradingCompany:
    pass


class Schedule:
    def __init__(self, vessel, steps=()):
        self.vessel = vessel
        self.steps = list(steps)

    def copy(self):
        return Schedule(self.vessel, self.steps)

    def get_insertion_points(self):
        return list(range(1, len(self.steps) + 2))

    def get_simple_schedule(self):
        return [("PICK_UP" if action == "pickup" else "DROP_OFF", trade)
                for action, trade in self.steps]

    def add_transportation(self, trade, location_pick_up=None, location_drop_off=None):
        pickup = location_pick_up if location_pick_up is not None else len(self.steps) + 1
        dropoff = location_drop_off if location_drop_off is not None else pickup
        if not (1 <= pickup <= len(self.steps) + 1 and pickup <= dropoff <= len(self.steps) + 1):
            raise ValueError("invalid insertion")
        self.steps.insert(pickup - 1, ("pickup", trade))
        self.steps.insert(dropoff, ("dropoff", trade))

    def verify_schedule(self):
        amount = 0
        aboard = set()
        for event, trade in self.steps:
            if event == "pickup":
                if trade in aboard:
                    return False
                amount += trade.amount
                aboard.add(trade)
            else:
                if trade not in aboard:
                    return False
                amount -= trade.amount
                aboard.remove(trade)
            if amount < 0 or amount > self.vessel.hold_capacity:
                return False
        return not aboard and amount == 0


class Vessel:
    loadable_cargo_types = ["Oil"]  # Test MABLE's property/list form, not just callable

    def __init__(self, name, location, speed=10.0, hold_capacity=100):
        self.name = name
        self.location = location
        self.speed = speed
        self.hold_capacity = hold_capacity
        self.schedule = Schedule(self)

    def current_load(self, cargo_type):
        return 0.0

    def capacity(self, cargo_type):
        return self.hold_capacity

    def get_travel_time(self, distance):
        return float(distance) / self.speed

    def get_loading_time(self, cargo_type, amount):
        return amount / 10.0

    def get_loading_consumption(self, hours):
        return hours

    def get_unloading_consumption(self, hours):
        return hours

    def get_idle_consumption(self, hours):
        return 0.5 * hours

    def get_laden_consumption(self, hours, speed):
        return 3.0 * hours

    def get_ballast_consumption(self, hours, speed):
        return 2.0 * hours

    def get_cost(self, consumption):
        return float(consumption)


class Headquarters:
    current_time = 0.0

    def get_network_distance(self, a, b):
        return abs(a.x - b.x)

    def get_companies(self):
        return []


class CostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
            cls.added_path = root
        else:
            cls.added_path = None
        cls.mock_modules = patch.dict(sys.modules, {
            "mable": types.ModuleType("mable"),
            "mable.cargo_bidding": types.ModuleType("mable.cargo_bidding"),
            "mable.transport_operation": types.ModuleType("mable.transport_operation"),
        })
        cls.mock_modules.start()
        sys.modules["mable.cargo_bidding"].TradingCompany = TradingCompany
        sys.modules["mable.transport_operation"].Bid = Bid
        sys.modules["mable.transport_operation"].ScheduleProposal = Proposal
        from source import cost_model
        from group6 import Companyn
        from source.bidding import BiddingStrategy
        cls.cost_model = cost_model
        cls.Company = Companyn
        cls.BiddingStrategy = BiddingStrategy

    @classmethod
    def tearDownClass(cls):
        cls.mock_modules.stop()
        if cls.added_path:
            sys.path.remove(cls.added_path)

    def setUp(self):
        self.start = Port("Start", 0)
        self.a = Port("A", 10)
        self.b = Port("B", 20)
        self.c = Port("C", 30)
        self.trade = Trade("T", self.a, self.b)
        self.vessel = Vessel("V", self.start)
        self.hq = Headquarters()
        self.company = self.Company()
        self.company.fleet = [self.vessel]
        self.company.headquarters = self.hq
        self.company.name = "Companyn"
        self.applied = []
        self.company.apply_schedules = lambda schedules: self.applied.append(schedules)

    def test_original_three_functions_import(self):
        self.assertTrue(callable(self.cost_model.estimate_trade_cost))
        self.assertTrue(callable(self.cost_model.estimate_schedule_cost))
        self.assertTrue(callable(self.cost_model.estimate_marginal_cost))

    def test_trade_cost_excludes_positioning(self):
        # Load 1 + laden 3 + unload 1 = 5.
        self.assertAlmostEqual(self.company.estimate_trade_cost(self.vessel, self.trade), 5.0)

    def test_schedule_cost_uses_headquarters_without_private_engine(self):
        schedule = self.vessel.schedule.copy()
        schedule.add_transportation(self.trade)
        # Ballast Start->A: 2, loading: 1, laden A->B: 3, unloading: 1.
        self.assertAlmostEqual(self.company.estimate_schedule_cost(self.vessel, schedule), 7.0)

    def test_marginal_cost(self):
        original = self.vessel.schedule
        new = original.copy()
        new.add_transportation(self.trade)
        self.assertAlmostEqual(self.cost_model.estimate_marginal_cost(
            self.vessel, original, new, self.hq), 7.0)

    def test_idle_wait(self):
        late_trade = Trade("T-late", self.a, self.b, earliest_pickup=5)
        schedule = self.vessel.schedule.copy()
        schedule.add_transportation(late_trade)
        # One hour to A, 4h waiting at idle=0.5 per hour, plus travel/transfer.
        self.assertAlmostEqual(self.company.estimate_schedule_cost(self.vessel, schedule), 9.0)

    def test_empty_schedule_cost(self):
        self.assertEqual(self.company.estimate_schedule_cost(self.vessel, self.vessel.schedule), 0.0)

    def test_person2_scheduler_receives_real_cost_function(self):
        scheduler = self.company.make_scheduler(time_budget_s=0.5)
        self.assertTrue(callable(scheduler.cost_fn))
        result = scheduler.plan([self.trade])
        self.assertEqual(result.scheduled_trades, [self.trade])
        self.assertAlmostEqual(result.costs[self.trade], 7.0)
        self.assertEqual(self.vessel.schedule.steps, [])

    def test_person3_bidder_uses_person1_direct_and_marginal(self):
        strategy = self.BiddingStrategy(self.company, pre_restarts=1, time_budget_s=1.0)
        self.assertAlmostEqual(strategy._direct_cost(self.vessel, self.trade), 5.0)
        result = strategy.make_bids([self.trade])
        self.assertEqual(len(result), 1)
        # Dedicated 7, marginal 7 = bid 7; no future bonus without pre_inform.
        self.assertAlmostEqual(result[0].amount, 7.0)

    def test_person4_callbacks_and_person2_receive_still_work(self):
        self.company.pre_inform([], 720)
        self.company.inform([self.trade])
        self.company.receive([types.SimpleNamespace(trade=self.trade, payment=10.0)], {})
        self.assertEqual(len(self.applied), 1)
        self.assertTrue(self.applied[0][self.vessel].verify_schedule())

    def test_missing_context_error_not_silently_1000(self):
        self.company.headquarters = None
        schedule = self.vessel.schedule.copy()
        schedule.add_transportation(self.trade)
        with self.assertRaises(self.cost_model.CostEstimationError):
            self.company.estimate_schedule_cost(self.vessel, schedule)


if __name__ == "__main__":
    unittest.main()
