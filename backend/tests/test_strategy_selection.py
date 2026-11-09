from apps.strategies.models import StrategyConfig


def test_runner_trades_the_enabled_strategy(runner):
    assert runner._strategy().strategy_id == "trend_pullback"
    assert not StrategyConfig.objects.get(strategy_id="asian_breakout").enabled  # created disabled
    StrategyConfig.objects.update(enabled=False)
    StrategyConfig.objects.filter(strategy_id="asian_breakout").update(enabled=True)
    runner.reload_if_changed()
    assert runner.agent.strategy.strategy_id == "asian_breakout"
    assert not runner._strategy_validated()
