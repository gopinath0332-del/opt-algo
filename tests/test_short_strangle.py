import pytest
from unittest.mock import MagicMock
from core.config import Config, StrategyConfig, MomentumFilterConfig
from strategy.short_strangle import ShortStrangleStrategy
from strategy.short_straddle import ShortStraddleStrategy
from api.rest_client import DeltaRestClient
from notifications.manager import NotificationManager


@pytest.fixture
def mock_dependencies():
    config = MagicMock(spec=Config)
    config.get_mode.return_value = "paper"
    config.enable_order_placement = False
    config.strategy = MagicMock(spec=StrategyConfig)

    client = MagicMock(spec=DeltaRestClient)
    notifier = MagicMock(spec=NotificationManager)

    return config, client, notifier


def test_config_loads_strangle_strategy():
    config = Config()
    strategies = config.get_straddle_strategies()
    strategy_names = [s.name for s in strategies]

    assert "btc_short_straddle" in strategy_names
    assert "btc_otm2_revmom_strangle" in strategy_names

    strangle_cfg = next(s for s in strategies if s.name == "btc_otm2_revmom_strangle")
    assert strangle_cfg.otm_steps == 2
    assert strangle_cfg.capital_allocation_pct == 30.0
    assert strangle_cfg.entry_time == "13:00"
    assert strangle_cfg.exit_time == "17:30"
    assert strangle_cfg.stop_loss is None
    assert strangle_cfg.momentum_filter is not None
    assert strangle_cfg.momentum_filter.enabled is True
    assert strangle_cfg.momentum_filter.reverse is True
    assert strangle_cfg.momentum_filter.threshold_pct == 0.5
    assert strangle_cfg.momentum_filter.lookback_hours == 2.0


def test_short_strangle_strategy_initialization(mock_dependencies):
    config, client, notifier = mock_dependencies

    strat_cfg = StrategyConfig(
        name="btc_otm2_revmom_strangle",
        underlying="BTC",
        entry_time="13:00",
        exit_time="17:30",
        capital_allocation_pct=30.0,
        otm_steps=2,
        momentum_filter=MomentumFilterConfig(
            enabled=True,
            reverse=True,
            lookback_hours=2.0,
            threshold_pct=0.5,
        ),
    )

    strategy = ShortStrangleStrategy(config, client, notifier, strategy_config=strat_cfg)

    assert strategy.strategy_display_name == "Short Strangle OTM+2"
    assert strategy.strategy_type_name == "short_strangle"
    assert strategy.capital_allocation_pct == 0.30
    assert strategy.underlying == "BTC"


def test_reverse_momentum_filter_triggers_skip_when_momentum_is_low(mock_dependencies):
    config, client, notifier = mock_dependencies

    strat_cfg = StrategyConfig(
        name="btc_otm2_revmom_strangle",
        underlying="BTC",
        otm_steps=2,
        momentum_filter=MomentumFilterConfig(
            enabled=True,
            reverse=True,
            lookback_hours=2.0,
            threshold_pct=0.5,
        ),
    )

    strategy = ShortStrangleStrategy(config, client, notifier, strategy_config=strat_cfg)

    # 0.2% move (< 0.5% threshold) -> in reverse mode, should SKIP (return True)
    client.get_candles.return_value = [
        {"open": 65000.0, "time": 100},
        {"close": 65130.0, "time": 200},  # +0.2% move
    ]

    should_skip = strategy._check_momentum_filter()
    assert should_skip is True
    notifier.send_status_message.assert_called_once()
    assert "Reverse Momentum Filter" in notifier.send_status_message.call_args[0][0]


def test_reverse_momentum_filter_allows_entry_when_momentum_is_high(mock_dependencies):
    config, client, notifier = mock_dependencies

    strat_cfg = StrategyConfig(
        name="btc_otm2_revmom_strangle",
        underlying="BTC",
        otm_steps=2,
        momentum_filter=MomentumFilterConfig(
            enabled=True,
            reverse=True,
            lookback_hours=2.0,
            threshold_pct=0.5,
        ),
    )

    strategy = ShortStrangleStrategy(config, client, notifier, strategy_config=strat_cfg)

    # 1.0% move (> 0.5% threshold) -> in reverse mode, should PROCEED (return False)
    client.get_candles.return_value = [
        {"open": 65000.0, "time": 100},
        {"close": 65650.0, "time": 200},  # +1.0% move
    ]

    should_skip = strategy._check_momentum_filter()
    assert should_skip is False
    notifier.send_status_message.assert_not_called()


def test_normal_momentum_filter_on_straddle(mock_dependencies):
    config, client, notifier = mock_dependencies

    strat_cfg = StrategyConfig(
        name="btc_short_straddle",
        underlying="BTC",
        otm_steps=0,
        momentum_filter=MomentumFilterConfig(
            enabled=True,
            reverse=False,
            lookback_hours=2.0,
            threshold_pct=1.2,
        ),
    )

    strategy = ShortStraddleStrategy(config, client, notifier, strategy_config=strat_cfg)

    # 1.5% move (> 1.2% threshold) -> in normal mode, should SKIP (return True)
    client.get_candles.return_value = [
        {"open": 65000.0, "time": 100},
        {"close": 65975.0, "time": 200},  # +1.5% move
    ]

    assert strategy._check_momentum_filter() is True
