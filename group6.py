from mable.cargo_bidding import TradingCompany
from mable.transport_operation import ScheduleProposal, Bid
from loguru import logger
from scheduler import SchedulingOptimizer

class Companyn(TradingCompany):
    def pre_inform(self, trades, time):
        pass

    def inform(self, trades, *args, **kwargs):
        proposed_scheduling = self.propose_schedules(trades)
        scheduled_trades = proposed_scheduling.scheduled_trades
        trades_and_costs = [
            (x, proposed_scheduling.costs[x]) if x in proposed_scheduling.costs
            else (x, 1000)
            for x in scheduled_trades]
        bids = [Bid(amount=cost, trade=one_trade) for one_trade, cost in trades_and_costs]
        return bids

    def receive(self, contracts, auction_ledger=None, *args, **kwargs):
        trades = [contract.trade for contract in contracts]

        proposal = self.propose_schedules(trades)

        missing = [
            trade for trade in trades
            if trade not in proposal.scheduled_trades
        ]

        rejected = self.apply_schedules(proposal.schedules)

        if missing or rejected:
            logger.warning(
                "Scheduling issues: missing={}, rejected={}",
                len(missing),
                rejected
            )

    def propose_schedules(self, trades):
        optimizer = SchedulingOptimizer(
            cost_function=self.estimate_schedule_cost
        )

        return optimizer.plan_schedules(
            fleet=self.fleet,
            trades=trades
        )
