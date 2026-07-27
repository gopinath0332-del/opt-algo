import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from api.rest_client import DeltaRestClient, APIError
from core.config import Config

@pytest.fixture
def client():
    config = MagicMock(spec=Config)
    config.base_url = "https://cdn-ind.testnet.deltaex.org"
    config.api_key = "dummy"
    config.api_secret = "dummy"
    config.environment = "testnet"
    return DeltaRestClient(config=config)

def test_find_atm_options_selects_nearest_expiry_strikes(client):
    now = datetime.now(timezone.utc)
    daily_expiry = (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    weekly_expiry = (now + timedelta(days=5)).isoformat().replace("+00:00", "Z")

    mock_products = [
        # Daily options expiring in 30 minutes (strikes 64400, 64600)
        {"id": 1, "symbol": "C-BTC-64400-DAILY", "contract_type": "call_options", "strike_price": "64400", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 2, "symbol": "P-BTC-64400-DAILY", "contract_type": "put_options", "strike_price": "64400", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 3, "symbol": "C-BTC-64600-DAILY", "contract_type": "call_options", "strike_price": "64600", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 4, "symbol": "P-BTC-64600-DAILY", "contract_type": "put_options", "strike_price": "64600", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        # Weekly options expiring in 5 days (has 64500 strike)
        {"id": 5, "symbol": "C-BTC-64500-WEEKLY", "contract_type": "call_options", "strike_price": "64500", "settlement_time": weekly_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 6, "symbol": "P-BTC-64500-WEEKLY", "contract_type": "put_options", "strike_price": "64500", "settlement_time": weekly_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
    ]

    client.get_option_products = MagicMock(return_value=mock_products)

    # Spot price is 64,481.44 (closer to 64500, but 64500 is only on weekly expiry)
    call_prod, put_prod, atm_strike = client.find_atm_options(underlying="BTC", spot_price=64481.44)

    assert atm_strike == 64400.0
    assert call_prod["symbol"] == "C-BTC-64400-DAILY"
    assert put_prod["symbol"] == "P-BTC-64400-DAILY"

def test_find_atm_options_rejects_weekly_and_monthly_contracts(client):
    now = datetime.now(timezone.utc)
    weekly_expiry = (now + timedelta(days=5)).isoformat().replace("+00:00", "Z")
    monthly_expiry = (now + timedelta(days=25)).isoformat().replace("+00:00", "Z")

    mock_products = [
        # Weekly options expiring in 5 days
        {"id": 1, "symbol": "C-BTC-64500-WEEKLY", "contract_type": "call_options", "strike_price": "64500", "settlement_time": weekly_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 2, "symbol": "P-BTC-64500-WEEKLY", "contract_type": "put_options", "strike_price": "64500", "settlement_time": weekly_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        # Monthly options expiring in 25 days
        {"id": 3, "symbol": "C-BTC-64500-MONTHLY", "contract_type": "call_options", "strike_price": "64500", "settlement_time": monthly_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 4, "symbol": "P-BTC-64500-MONTHLY", "contract_type": "put_options", "strike_price": "64500", "settlement_time": monthly_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
    ]

    client.get_option_products = MagicMock(return_value=mock_products)

    # Should raise error because no daily contract (<= 24h) exists
    with pytest.raises(APIError, match="No active daily option contract"):
        client.find_atm_options(underlying="BTC", spot_price=64481.44)

def test_find_atm_options_exact_match(client):
    now = datetime.now(timezone.utc)
    daily_expiry = (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")

    mock_products = [
        {"id": 1, "symbol": "C-BTC-64400-DAILY", "contract_type": "call_options", "strike_price": "64400", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
        {"id": 2, "symbol": "P-BTC-64400-DAILY", "contract_type": "put_options", "strike_price": "64400", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "BTC"}},
    ]

    client.get_option_products = MagicMock(return_value=mock_products)

    call_prod, put_prod, atm_strike = client.find_atm_options(underlying="BTC", spot_price=64400.00)

    assert atm_strike == 64400.0
    assert call_prod["symbol"] == "C-BTC-64400-DAILY"
    assert put_prod["symbol"] == "P-BTC-64400-DAILY"

def test_find_atm_options_no_products(client):
    client.get_option_products = MagicMock(return_value=[])

    with pytest.raises(APIError, match="No live option products found"):
        client.find_atm_options(underlying="BTC", spot_price=64400.00)


def test_find_atm_options_xaut(client):
    now = datetime.now(timezone.utc)
    daily_expiry = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    mock_products = [
        {"id": 101, "symbol": "C-XAUT-4050-270726", "contract_type": "call_options", "strike_price": "4050", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "XAUT"}},
        {"id": 102, "symbol": "P-XAUT-4050-270726", "contract_type": "put_options", "strike_price": "4050", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "XAUT"}},
        {"id": 103, "symbol": "C-XAUT-4060-270726", "contract_type": "call_options", "strike_price": "4060", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "XAUT"}},
        {"id": 104, "symbol": "P-XAUT-4060-270726", "contract_type": "put_options", "strike_price": "4060", "settlement_time": daily_expiry, "state": "live", "underlying_asset": {"symbol": "XAUT"}},
    ]

    client.get_option_products = MagicMock(return_value=mock_products)

    call_prod, put_prod, atm_strike = client.find_atm_options(underlying="XAUT", spot_price=4064.10)

    assert atm_strike == 4060.0
    assert call_prod["symbol"] == "C-XAUT-4060-270726"
    assert put_prod["symbol"] == "P-XAUT-4060-270726"

