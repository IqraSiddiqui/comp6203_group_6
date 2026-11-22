"""MABLE-free integration checks, from repository root:
python -m unittest discover -s tests -p 'unittest_future_planning.py' -v
"""
import sys
import types
import unittest
from dataclasses import dataclass
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
    cargo_type: str = 'Oil'
    earliest_pickup: float = 0.0
    latest_drop_off: float = 2000.0


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


class FakeSchedule:
    def __init__(self, vessel, steps=()):
        self.vessel = vessel
        self.steps = list(steps)

    def copy(self):
        return FakeSchedule(self.vessel, self.steps)

    def get_insertion_points(self):
        return list(range(1, len(self.steps) + 2))

    def get_simple_schedule(self):
        return [(('PICK_UP' if action == 'pickup' else 'DROP_OFF'), trade)
                for action, trade in self.steps]

    def add_transportation(self, trade, location_pick_up=None, location_drop_off=None):
        pickup = location_pick_up if location_pick_up is not None else len(self.steps) + 1
        dropoff = location_drop_off if location_drop_off is not None else pickup
        if not (1 <= pickup <= len(self.steps)+1 and pickup <= dropoff <= len(self.steps)+1):
            raise ValueError('bad insertion')
        self.steps.insert(pickup-1, ('pickup', trade))
        self.steps.insert(dropoff, ('dropoff', trade))

    def verify_schedule(self):
        onboard = 0
        seen = set()
        for action, trade in self.steps:
            if action == 'pickup':
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
    def __init__(self, name, location, speed=10.0, hold_capacity=100):
        self.name = name
        self.location = location
        self.speed = speed
        self.hold_capacity = hold_capacity
        self.schedule = FakeSchedule(self)

    def capacity(self, cargo_type):
        return self.hold_capacity

    def get_travel_time(self, distance):
        return distance / self.speed

    def get_loading_time(self, cargo_type, amount):
        return amount / 10

    def get_loading_consumption(self, hours):
        return hours

    def get_unloading_consumption(self, hours):
        return hours

    def get_laden_consumption(self, hours, speed):
        return hours * 3

    def get_ballast_consumption(self, hours, speed):
        return hours * 2

    def get_cost(self, consumption):
        return float(consumption)
    def loadable_cargo_types(self):
        return ["Oil"]

    def current_load(self, cargo_type):
        return 0.0


class FakeNetwork:
    def __init__(self, companies):
        self.companies = companies

    def get_companies(self):
        return self.companies

    def get_network_distance(self, a, b):
        return abs(a.x - b.x)


class FuturePlanningIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project_root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(project_root))
        cls._project_root = str(project_root)
        mable = types.ModuleType('mable')
        cb = types.ModuleType('mable.cargo_bidding')
        cb.TradingCompany = FakeTradingCompany
        operation = types.ModuleType('mable.transport_operation')
        operation.Bid = FakeBid
        operation.ScheduleProposal = FakeProposal
        cls._mocks = patch.dict(sys.modules, {
            'mable': mable,
            'mable.cargo_bidding': cb,
            'mable.transport_operation': operation,
        })
        cls._mocks.start()
        from group6 import Companyn
        cls.Company = Companyn

    @classmethod
    def tearDownClass(cls):
        cls._mocks.stop()
        if cls._project_root in sys.path:
            sys.path.remove(cls._project_root)

    def setUp(self):
        self.company = self.Company()
        self.company.name = 'Companyn'
        self.company.fleet = [FakeVessel('mine', Port('Start', 0))]
        self.enemy = types.SimpleNamespace(name='Competitor', fleet=[FakeVessel('enemy', Port('Other', 100))])
        self.company.headquarters = FakeNetwork([self.company, self.enemy])
        self.company.headquarters.current_time = 0.0
        self.applied = []
        self.company.apply_schedules = lambda schedules: self.applied.append(schedules)
        self.trade = Trade('current', Port('A', 10), Port('B', 20))
        self.future = Trade('future', Port('C', 21), Port('D', 30))

    def test_pre_inform_stores_trades(self):
        self.company.pre_inform([self.future], 720)
        self.assertEqual(self.company._future_trades, [self.future])
        self.assertEqual(self.company._future_auction_time, 720)

    def test_future_bonus_nonzero_and_capped(self):
        self.company.pre_inform([self.future], 720)
        self.company.snapshot_competitors()
        value = self.company.future_trade_bonus(self.company.fleet[0], self.trade, cost=10)
        self.assertGreater(value, 0)
        self.assertLessEqual(value, 3.0)

    def test_future_bonus_affects_bids_when_enabled(self):
        self.company.pre_inform([self.future], 720)
        self.company.enable_future_bid_bonus = False
        baseline = self.company.inform([self.trade])
        self.company.enable_future_bid_bonus = True
        discounted = self.company.inform([self.trade])
        self.assertEqual(len(baseline), 1)
        self.assertEqual(len(discounted), 1)
        self.assertLess(discounted[0].amount, baseline[0].amount)
        self.assertEqual(len(self.company._p4_our_bids), 1)

    def test_competitor_costs_and_capacity(self):
        self.company.snapshot_competitors()
        costs = self.company.competitor_costs(self.trade)
        self.assertIn('Competitor', costs)
        huge = Trade('huge', Port('A', 10), Port('B', 20), amount=10000)
        self.assertNotIn('Competitor', self.company.competitor_costs(huge))

    def test_auction_ledger_updates_while_scheduler_runs(self):
        self.company.inform([self.trade])
        estimated = self.company._p4_standalone_cost(self.enemy.fleet[0], self.trade)
        ledger = {'Competitor': [types.SimpleNamespace(trade=self.trade, payment=estimated*1.4)]}
        self.company.receive([], ledger)
        self.assertEqual(self.company.opponent_summary()['samples']['Competitor'], 1)
        self.assertEqual(len(self.applied), 0)

    def test_existing_receive_not_replaced(self):
        self.company.inform([self.trade])
        self.company.receive([types.SimpleNamespace(trade=self.trade, payment=90)], None)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0][self.company.fleet[0]].steps[-1], ('dropoff', self.trade))


if __name__ == '__main__':
    unittest.main()
