import json

from django.core.management.base import BaseCommand

from apps.analytics.models import BacktestRun
from apps.analytics.services import execute_backtest


class Command(BaseCommand):
    help = "Run a backtest / walk-forward from the command line and store it."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="EURUSD")
        parser.add_argument("--source", choices=["csv", "gateway", "synthetic"], default="csv")
        parser.add_argument("--path", default="", help="CSV path relative to DATA_DIR")
        parser.add_argument("--timeframe", default="M15")
        parser.add_argument("--walk-forward", action="store_true")
        parser.add_argument("--grid", default="[]", help='JSON list of param overrides, e.g. [{"rr_target":1.5},{"rr_target":2}]')
        parser.add_argument("--spread-points", type=float, default=None)
        parser.add_argument("--commission", type=float, default=3.5)
        parser.add_argument("--slippage-points", type=float, default=1.0)

    def handle(self, *args, **o):
        run = BacktestRun.objects.create(
            kind="WALK_FORWARD" if o["walk_forward"] else "SINGLE", symbol=o["symbol"], data_source=o["source"], data_path=o["path"],
            base_timeframe=o["timeframe"], param_grid=json.loads(o["grid"]),
            costs={"spread_points": o["spread_points"], "commission_per_lot_side": o["commission"], "slippage_points": o["slippage_points"]},
        )
        run = execute_backtest(run.pk)
        self.stdout.write(json.dumps({"id": run.pk, "status": run.status, "validation": run.validation_status, "metrics": run.metrics,
                                      "reasons": run.reasons, "error": run.error}, indent=2, default=str))
