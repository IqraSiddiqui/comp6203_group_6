from mable.cargo_bidding import TradingCompany
from mable.transport_operation import ScheduleProposal
from loguru import logger
from source.scheduler import SchedulingOptimizer
from source.bidding import BiddingStrategy
from source.future_planning import FuturePlanningMixin

class Companyn(FuturePlanningMixin, TradingCompany):
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
            logger.exception("Error in Companyn.inform()")
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
                rejected = self.apply_schedules(result.schedules)
                if rejected:
                    logger.warning("MABLE rejected schedule(s): {}", rejected)

    def make_scheduler(self, time_budget_s=8.0):
        cost_fn = getattr(self, "estimate_schedule_cost", None)

        return SchedulingOptimizer(
            fleet=self.fleet,
            cost_fn=cost_fn,
            restarts=8,
            time_budget_s=time_budget_s,
        )

    def propose_schedules(self, trades):
        """pre-auction minimum-cost feasible planning"""
        result = self.make_scheduler(time_budget_s=8.0).plan(trades)
        return ScheduleProposal(
            result.schedules, result.scheduled_trades, result.costs
        )
