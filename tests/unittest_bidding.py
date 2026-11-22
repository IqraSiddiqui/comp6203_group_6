"""Tests for the standalone bidding adapter without requiring MABLE installed.

Run from project directory:
    python -m unittest -v test_bidding_integration.py
"""

from dataclasses import dataclass
from pathlib import Path
import importlib.util
import sys
import types
import unittest
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


@dataclass
class FakeBid:
    amount: float
    trade: object


@dataclass
class FakeProposal:
    schedules: dict
    scheduled_trades: list
    costs: dict


class FakeTradingCompany:
    pass


class FakeNetwork:
    current_time = 0.0

    def get_companies(self):
        return []

    def get_network_distance(self, a, b):
        return abs(a.x - b.x)


class FakeSchedule:
    def __init__(self, vessel, steps=()):
        self.vessel = vessel
        self.steps = list(steps)

    def copy(self):
        return FakeSchedule(self.vessel, self.steps)

    def get_insertion_points(self):
        return list(range(1, len(self.steps) + 2))

    def get_simple_schedule(self):
        return [("PICK_UP" if a == "pickup" else "DROP_OFF", t)
                for a, t in self.steps]

    def add_transportation(self, trade, location_pick_up=None, location_drop_off=None):
        pickup = location_pick_up if location_pick_up is not None else len(self.steps) + 1
        dropoff = location_drop_off if location_drop_off is not None else pickup
        if not (1 <= pickup <= len(self.steps) + 1 and pickup <= dropoff <= len(self.steps) + 1):
            raise ValueError("bad insertion")
        self.steps.insert(pickup - 1, ("pickup", trade))
        self.steps.insert(dropoff, ("dropoff", trade))

    def verify_schedule(self):
        onboard = 0
        seen = set()
        for action, trade in self.steps:
            if action == "pickup":
                if trade in seen:
                    return False
                onboard += trade.amount
                seen.add(trade)
            else:
                if trade not in seen:
                    return False
                onboard -= trade.amount
            if onboard > self.vessel.hold_capacity or onboard < 0:
                return False
        return onboard == 0


class FakeVessel:
    def __init__(self, name, location, speed=10, hold_capacity=100):
        self.name = name
        self.location = location
        self.speed = speed
        self.hold_capacity = hold_capacity
        self.schedule = FakeSchedule(self)

    def loadable_cargo_types(self):
        return ["Oil"]

    def current_load(self, cargo_type):
        return 0.0

    def get_loading_time(self, cargo_type, amount):
        return amount / 10

    def get_loading_consumption(self, hours):
        return hours

    def get_unloading_consumption(self, hours):
        return hours

    def get_laden_consumption(self, hours, speed):
        return 3 * hours

    def get_ballast_consumption(self, hours, speed):
        return 2 * hours

    def get_cost(self, consumption):
        return float(consumption)
    
    def current_load(self, cargo_type):
        return 0.0

    def get_travel_time(self, distance):
        return float(distance) / self.speed

    def get_idle_consumption(self, hours):
        return 0.5 * hours


class BiddingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mable = types.ModuleType("mable")
        cb = types.ModuleType("mable.cargo_bidding")
        cb.TradingCompany = FakeTradingCompany
        operation = types.ModuleType("mable.transport_operation")
        operation.Bid = FakeBid
        operation.ScheduleProposal = FakeProposal
        cls.mable_modules = {
            "mable": mable,
            "mable.cargo_bidding": cb,
            "mable.transport_operation": operation,
        }
        cls.patcher = patch.dict(sys.modules, cls.mable_modules)
        cls.patcher.start()
        import source.bidding as bidding
        cls.bidding = bidding
        project_root = Path(__file__).resolve().parent.parent
        agent_path = project_root / "group6.py"

        spec = importlib.util.spec_from_file_location(
            "group6_adapter_for_test", agent_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        cls.Company = mod.Companyn
        spec = importlib.util.spec_from_file_location(
            "group6_adapter_for_test", agent_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.Company = mod.Companyn

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()

    def setUp(self):
        self.company = self.Company()
        self.company.fleet = [FakeVessel("v1", Port("Start", 0))]
        self.company.headquarters = FakeNetwork()
        self.applied = []
        self.company.apply_schedules = lambda schedules: self.applied.append(schedules)
        self.a = Trade("a", Port("A", 10), Port("B", 20))
        self.b = Trade("b", Port("B", 20), Port("C", 30))

    def test_bids_without_person1_cost_fn(self):
        bids = self.company.inform([self.a, self.b])
        self.assertTrue(bids)
        self.assertTrue(all(b.amount > 0 for b in bids))
        self.assertTrue(all(b.amount != 1000 for b in bids))
        self.assertEqual(self.company.fleet[0].schedule.steps, [])

    def test_receive_preserves_person2_scheduling(self):
        self.company.inform([self.a])
        self.company.receive([types.SimpleNamespace(trade=self.a, payment=90)])
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0][self.company.fleet[0]].steps[-1], ("dropoff", self.a))

    def test_person1_cost_fn_is_used_by_scheduler_and_marginal_bids(self):
        before = self.company.inform([self.a])
        seen = []
        def estimate(vessel, schedule):
            seen.append(len(schedule.steps))
            return float(len(schedule.steps) * 10)
        self.company.estimate_schedule_cost = estimate
        after = self.company.inform([self.a])
        self.assertTrue(after)
        self.assertTrue(seen)
        self.assertNotAlmostEqual(before[0].amount, after[0].amount)

    def test_p_select_filters_low_frequency(self):
        p = self.bidding._CandidatePlan
        v = self.company.fleet[0]
        a_id, b_id = id(self.a), id(self.b)
        plans = [
            p((1,), {a_id: v, b_id: v}, {a_id: 20.0, b_id: 10.0}, 2, 1),
            p((2,), {a_id: v}, {a_id: 20.0}, 1, 1),
            p((3,), {a_id: v}, {a_id: 20.0}, 1, 1),
        ]
        strategy = self.bidding.BiddingStrategy(self.company)
        with patch.object(strategy, '_top_plans', return_value=plans):
            bids = strategy.make_bids([self.a, self.b])
        self.assertEqual([x.trade for x in bids], [self.a])

    def test_oversized_trade_not_bid(self):
        impossible = Trade("impossible", Port("A", 10), Port("B", 20), amount=1000)
        self.assertEqual(self.company.inform([impossible]), [])


if __name__ == '__main__':
    unittest.main()
