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
        """schedule actual awarded trades, preserving commitments"""
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
                rejected = self.apply_schedules(result.schedules)
                if rejected:
                    logger.warning("MABLE rejected schedule(s): {}", rejected)

    def propose_schedules(self, trades):
        """pre-auction minimum-cost feasible planning"""
        result = self.make_scheduler(time_budget_s=8.0).plan(trades)
        return ScheduleProposal(
            result.schedules, result.scheduled_trades, result.costs
        )
