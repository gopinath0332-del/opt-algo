"""REST API client for Delta Exchange Options using delta-rest-client library."""

import os
import time
import json
import hmac
import random
import hashlib
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from delta_rest_client import DeltaRestClient as BaseDeltaClient, OrderType

from core.config import Config
from core.exceptions import APIError, AuthenticationError, RateLimitError
from core.logger import get_logger

from .rate_limiter import RateLimiter

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Retry / backoff helpers
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_MAX_RETRIES: int = int(os.getenv("API_MAX_RETRIES", "4"))
_BACKOFF_BASE: float = float(os.getenv("API_BACKOFF_BASE_SEC", "2"))
_BACKOFF_MAX: float = float(os.getenv("API_BACKOFF_MAX_SEC", "60"))


def _backoff_wait(attempt: int) -> None:
    """Sleep for an exponentially increasing duration with random jitter."""
    delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
    jitter = random.uniform(0, 1)
    total_wait = delay + jitter
    logger.warning(
        f"API retry backoff: waiting {total_wait:.1f}s "
        f"(attempt {attempt + 1}/{_MAX_RETRIES}, base={_BACKOFF_BASE}s, cap={_BACKOFF_MAX}s)"
    )
    time.sleep(total_wait)


class DeltaRestClient:
    """Wrapper around delta-rest-client library with options-specific features.

    Features:
    - Automatic rate limiting
    - Retry logic with exponential backoff
    - Options chain discovery (find ATM strike, call/put products)
    - Structured logging
    - Error handling
    """

    def __init__(self, config: Config):
        """Initialize Delta Exchange REST API client.

        Args:
            config: Configuration instance
        """
        self.config = config
        self.rate_limiter = RateLimiter(max_requests=150, time_window=300)
        self.time_offset = 0

        try:
            self.client = BaseDeltaClient(
                base_url=config.base_url, api_key=config.api_key, api_secret=config.api_secret
            )
            logger.info(
                "Delta REST client initialized",
                base_url=config.base_url,
                environment=config.environment,
            )
        except Exception as e:
            logger.error("Failed to initialize Delta REST client", error=str(e))
            raise AuthenticationError(f"Failed to initialize client: {e}")

    def _make_request(self, func, *args, **kwargs) -> Any:
        """Make API request with rate limiting and error handling."""
        self.rate_limiter.wait_if_needed()

        try:
            response = func(*args, **kwargs)
            return response
        except Exception as e:
            error_msg = str(e)
            if "rate limit" in error_msg.lower():
                raise RateLimitError(f"Rate limit exceeded: {error_msg}")
            elif "unauthorized" in error_msg.lower() or "authentication" in error_msg.lower():
                raise AuthenticationError(f"Authentication failed: {error_msg}")
            else:
                raise APIError(f"API request failed: {error_msg}")

    def _generate_signature(self, method: str, endpoint: str, payload: str, timestamp: str) -> str:
        """Generate HMAC-SHA256 signature."""
        msg = f"{method}{timestamp}{endpoint}{payload}"
        signature = hmac.new(
            self.config.api_secret.encode('utf-8'),
            msg.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _make_auth_request(self, method: str, endpoint: str, params: Optional[Dict] = None, data: Optional[Dict] = None) -> Any:
        """Make authenticated API request directly."""
        import requests

        self.rate_limiter.wait_if_needed()

        url = f"{self.config.base_url}{endpoint}"

        max_retries = 1
        for attempt in range(max_retries + 1):
            timestamp = str(int(time.time() + self.time_offset))

            query_string = ""
            if params:
                query_string = urllib.parse.urlencode(params)
                url_with_query = f"{url}?{query_string}"
            else:
                url_with_query = url

            payload = ""
            if data:
                payload = json.dumps(data)

            path_with_query = endpoint
            if query_string:
                path_with_query = f"{endpoint}?{query_string}"

            signature = self._generate_signature(method, path_with_query, payload, timestamp)

            headers = {
                "Content-Type": "application/json",
                "api-key": self.config.api_key,
                "timestamp": timestamp,
                "signature": signature
            }

            try:
                if method == "GET":
                    response = requests.get(url_with_query, headers=headers, timeout=30)
                elif method == "POST":
                    response = requests.post(url_with_query, headers=headers, data=payload, timeout=30)
                elif method == "DELETE":
                    response = requests.delete(url_with_query, headers=headers, timeout=30)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                if response.status_code == 401:
                    try:
                        resp_json = response.json()
                        error_data = resp_json.get("error", {})
                        if error_data.get("code") == "expired_signature" and attempt < max_retries:
                            context = error_data.get("context", {})
                            server_time = context.get("server_time")
                            if server_time:
                                local_time = int(time.time())
                                diff = server_time - local_time
                                self.time_offset = diff + 2
                                logger.warning(f"Time drift detected. Syncing clock. Offset: {self.time_offset}s")
                                continue
                    except Exception:
                        pass

                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                if attempt == max_retries:
                    logger.error(f"Auth API request failed: {endpoint}", error=str(e))
                    if e.response is not None:
                        logger.error(f"Response: {e.response.text}")
                    raise APIError(f"Auth request failed: {e}")

    def _make_direct_request(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """Make direct API request for public endpoints.

        Implements exponential backoff with jitter on transient failures.
        """
        import requests

        self.rate_limiter.wait_if_needed()

        url = f"{self.config.base_url}{endpoint}"
        last_exception: Optional[Exception] = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=30)

                if response.status_code == 401:
                    logger.error(f"Direct API auth error (401) for {endpoint} – not retrying")
                    response.raise_for_status()

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    logger.warning(
                        f"Retryable HTTP {response.status_code} from {endpoint}. "
                        f"Attempt {attempt + 1}/{_MAX_RETRIES + 1}"
                    )
                    if attempt < _MAX_RETRIES:
                        _backoff_wait(attempt)
                        continue
                    response.raise_for_status()

                if response.status_code == 400:
                    logger.warning(
                        f"HTTP 400 Bad Request from {endpoint} – exchange may be busy. "
                        f"Attempt {attempt + 1}/{_MAX_RETRIES + 1}"
                    )
                    if attempt < _MAX_RETRIES:
                        _backoff_wait(attempt)
                        continue
                    response.raise_for_status()

                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout as e:
                last_exception = e
                logger.warning(
                    f"Request timeout for {endpoint}. "
                    f"Attempt {attempt + 1}/{_MAX_RETRIES + 1}"
                )
                if attempt < _MAX_RETRIES:
                    _backoff_wait(attempt)
                    continue

            except requests.exceptions.ConnectionError as e:
                last_exception = e
                logger.warning(
                    f"Connection error for {endpoint}. "
                    f"Attempt {attempt + 1}/{_MAX_RETRIES + 1}"
                )
                if attempt < _MAX_RETRIES:
                    _backoff_wait(attempt)
                    continue

            except requests.exceptions.RequestException as e:
                logger.error("Direct API request failed", endpoint=endpoint, error=str(e))
                raise APIError(f"API request failed: {e}")

        logger.error(
            f"Direct API request failed after {_MAX_RETRIES} retries: {endpoint}",
            error=str(last_exception),
        )
        raise APIError(f"API request failed after {_MAX_RETRIES} retries: {last_exception}")

    # -----------------------------------------------------------------------
    # Product and Market Data Methods
    # -----------------------------------------------------------------------

    def get_products(self, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Get list of all available products.

        Args:
            params: Optional query parameters for filtering

        Returns:
            List of product dictionaries
        """
        logger.debug("Fetching products")
        response = self._make_direct_request("/v2/products", params=params)
        products = response.get("result", [])
        logger.info("Fetched products", count=len(products))
        return cast(List[Dict[str, Any]], products)

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker data for a symbol.

        Args:
            symbol: Trading symbol (e.g., 'BTCUSD')

        Returns:
            Ticker data
        """
        logger.debug("Fetching ticker", symbol=symbol)
        response = self._make_request(self.client.get_ticker, symbol)
        return cast(Dict[str, Any], response)

    def get_spot_price(self, underlying: str = "BTC") -> float:
        """Get the current spot/mark price for an underlying.

        Uses the perpetual futures product (e.g., BTCUSD) as the spot reference.

        Args:
            underlying: Underlying asset (e.g., 'BTC', 'ETH')

        Returns:
            Current mark price
        """
        symbol = f"{underlying}USD"
        ticker = self.get_ticker(symbol)
        mark_price = ticker.get("mark_price") or ticker.get("close") or ticker.get("last_price")
        if mark_price is None:
            raise APIError(f"Could not get spot price for {symbol}")
        price = float(mark_price)
        logger.info(f"Spot price for {underlying}: ${price:,.2f}")
        return price

    # -----------------------------------------------------------------------
    # Options-Specific Methods
    # -----------------------------------------------------------------------

    def get_option_products(self, underlying: str = "BTC") -> List[Dict[str, Any]]:
        """Get all live option products for an underlying.

        Args:
            underlying: Underlying asset (e.g., 'BTC')

        Returns:
            List of option product dicts (calls and puts)
        """
        logger.info(f"Fetching option products for {underlying}")
        all_products = self.get_products()

        option_products = [
            p for p in all_products
            if p.get("contract_type") in ["call_options", "put_options"]
            and p.get("state") == "live"
            and p.get("underlying_asset", {}).get("symbol", "").upper() == underlying.upper()
        ]

        logger.info(f"Found {len(option_products)} live option products for {underlying}")
        return option_products

    def find_atm_options(
        self, underlying: str = "BTC", spot_price: Optional[float] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
        """Find the ATM Call and Put options for the nearest expiry.

        Args:
            underlying: Underlying asset
            spot_price: Current spot price (fetched automatically if not provided)

        Returns:
            Tuple of (call_product, put_product, atm_strike)

        Raises:
            APIError: If suitable options cannot be found
        """
        if spot_price is None:
            spot_price = self.get_spot_price(underlying)

        option_products = self.get_option_products(underlying)

        if not option_products:
            raise APIError(f"No live option products found for {underlying}")

        # Get all unique strikes
        strikes = set()
        for p in option_products:
            strike = p.get("strike_price")
            if strike is not None:
                strikes.add(float(strike))

        if not strikes:
            raise APIError("No strike prices found in option products")

        # Find ATM strike (closest to spot price)
        atm_strike = min(strikes, key=lambda s: abs(s - spot_price))
        logger.info(f"ATM strike for {underlying}: {atm_strike} (spot: {spot_price:,.2f})")

        # Filter options at ATM strike
        atm_options = [
            p for p in option_products
            if float(p.get("strike_price", 0)) == atm_strike
        ]

        # Separate calls and puts
        calls = [p for p in atm_options if p.get("contract_type") == "call_options"]
        puts = [p for p in atm_options if p.get("contract_type") == "put_options"]

        if not calls:
            raise APIError(f"No call option found at strike {atm_strike}")
        if not puts:
            raise APIError(f"No put option found at strike {atm_strike}")

        # Pick the nearest expiry for both
        def expiry_key(p):
            """Sort by settlement_time to find nearest expiry."""
            settlement = p.get("settlement_time") or p.get("expiry_date") or ""
            try:
                if isinstance(settlement, (int, float)):
                    return settlement
                return datetime.fromisoformat(str(settlement).replace("Z", "+00:00")).timestamp()
            except Exception:
                return float("inf")

        calls.sort(key=expiry_key)
        puts.sort(key=expiry_key)

        call_product = calls[0]
        put_product = puts[0]

        logger.info(
            f"Selected ATM options — "
            f"Call: {call_product.get('symbol')} (ID: {call_product.get('id')}), "
            f"Put: {put_product.get('symbol')} (ID: {put_product.get('id')})"
        )

        return call_product, put_product, atm_strike

    # -----------------------------------------------------------------------
    # Trading Methods
    # -----------------------------------------------------------------------

    def get_wallet_balance(self) -> Dict[str, Any]:
        """Get wallet balance for all assets."""
        logger.debug("Fetching wallet balance")
        response = self._make_auth_request("GET", "/v2/wallet/balances")
        return cast(Dict[str, Any], response)

    def get_available_balance(self) -> float:
        """Get available USD/USDT balance from the wallet.

        Parses the /v2/wallet/balances response and returns the available
        balance for the USD asset. Falls back to searching for 'USDT' if
        'USD' is not found.

        Returns:
            Available balance in USD, or 0.0 if it cannot be determined.
        """
        try:
            response = self.get_wallet_balance()
            balances = response.get("result", [])
            if not isinstance(balances, list):
                logger.warning("Unexpected wallet balance format", response=response)
                return 0.0

            # Try to find USD or USDT asset
            for asset in balances:
                symbol = str(asset.get("asset_symbol", "") or asset.get("asset", {}).get("symbol", "")).upper()
                if symbol in ("USD", "USDT"):
                    available = float(asset.get("available_balance", 0) or 0)
                    logger.info(f"Wallet available balance ({symbol}): ${available:,.2f}")
                    return available

            logger.warning("USD/USDT asset not found in wallet balances")
            return 0.0
        except Exception as e:
            logger.error(f"Failed to fetch available balance: {e}")
            return 0.0

    def get_order_margin(
        self,
        product_id: int,
        size: int,
        side: str = "sell",
        order_type: str = "market_order",
    ) -> Optional[float]:
        """Query the exchange for the actual margin required for an order.

        Calls POST /v2/orders/compute_margin which mirrors the exchange UI's
        "Order Margin" value and accounts for premium offset on short options.

        Args:
            product_id: Option product ID.
            size: Number of lots.
            side: 'buy' or 'sell'.
            order_type: 'market_order' or 'limit_order'.

        Returns:
            Margin in USD, or None if the request fails.
        """
        try:
            data = {
                "product_id": product_id,
                "size": size,
                "side": side,
                "order_type": order_type,
            }
            response = self._make_auth_request("POST", "/v2/orders/compute_margin", data=data)
            result = response.get("result", {})
            if result is None:
                return None
            margin = result.get("order_margin")
            if margin is not None:
                return float(margin)
            logger.warning("compute_margin response missing 'order_margin'", response=response)
            return None
        except Exception as e:
            logger.warning(f"get_order_margin failed: {e}")
            return None

    def get_wallet_transactions(
        self,
        transaction_types: str,
        start_time_us: int,
        end_time_us: int,
        asset_id: Optional[int] = None,
        product_id: Optional[int] = None,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch all wallet transactions of a given type within a time range.

        Paginates through all results using the 'after' cursor until exhausted.

        Args:
            transaction_types: Transaction type filter (e.g. 'funding', 'commission')
            start_time_us: Start time in microseconds (epoch)
            end_time_us: End time in microseconds (epoch)
            asset_id: Optional asset ID to filter transactions
            product_id: Optional product ID to filter transactions (locally)
            page_size: Number of records per page (max 100)

        Returns:
            List of transaction dicts (empty list on any failure)
        """
        logger.info(
            "Fetching wallet transactions",
            transaction_types=transaction_types,
            start_us=start_time_us,
            end_us=end_time_us,
        )

        params: Dict[str, Any] = {
            "transaction_types": transaction_types,
            "start_time": start_time_us,
            "end_time": end_time_us,
            "page_size": page_size,
        }
        if asset_id is not None:
            params["asset_ids"] = str(asset_id)

        all_transactions: List[Dict[str, Any]] = []
        after_cursor: Optional[str] = None

        while True:
            if after_cursor:
                params["after"] = after_cursor
            elif "after" in params:
                del params["after"]

            try:
                response = self._make_auth_request("GET", "/v2/wallet/transactions", params=params)
            except Exception as e:
                logger.warning("Failed to fetch wallet transactions", transaction_types=transaction_types, error=str(e))
                break

            result = response.get("result", []) if isinstance(response, dict) else []
            if not result:
                break

            all_transactions.extend(result)

            meta = response.get("meta", {}) if isinstance(response, dict) else {}
            after_cursor = meta.get("after")
            if not after_cursor:
                break

        # Filter locally by product_id if provided
        if product_id is not None:
            all_transactions = [
                t for t in all_transactions 
                if str(t.get("product_id")) == str(product_id)
            ]

        logger.info("Fetched and filtered wallet transactions", transaction_types=transaction_types, count=len(all_transactions), product_id=product_id)
        return all_transactions

    def get_funding_transactions(
        self,
        start_time_us: int,
        end_time_us: int,
        asset_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch funding rate wallet transactions. Wrapper around get_wallet_transactions."""
        return self.get_wallet_transactions("funding", start_time_us, end_time_us, asset_id, product_id)

    def get_trading_fee_transactions(
        self,
        start_time_us: int,
        end_time_us: int,
        asset_id: Optional[int] = None,
        product_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch trading fee wallet transactions. Wrapper around get_wallet_transactions."""
        return self.get_wallet_transactions("commission", start_time_us, end_time_us, asset_id, product_id)

    def get_positions(self, product_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get open positions.

        Args:
            product_id: Optional product ID to filter

        Returns:
            List of open positions
        """
        logger.debug("Fetching positions", product_id=product_id)
        try:
            params = {}
            if product_id:
                params['product_ids'] = str(product_id)

            response = self._make_auth_request("GET", "/v2/positions/margined", params=params)
            return cast(List[Dict[str, Any]], response.get('result', []))
        except Exception:
            raise

    def get_position(self, product_id: int) -> Dict[str, Any]:
        """Get position for a specific product."""
        logger.debug("Fetching position", product_id=product_id)
        response = self._make_request(self.client.get_position, product_id)
        return cast(Dict[str, Any], response)

    def place_order(
        self,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        limit_price: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Place a new order with automatic retry on transient errors.

        Args:
            product_id: Product ID
            size: Order size (number of contracts)
            side: 'buy' or 'sell'
            order_type: Order type ('limit_order' or 'market_order')
            limit_price: Limit price (required for limit orders)
            **kwargs: Additional order parameters

        Returns:
            Order response dict

        Raises:
            APIError: If all retries are exhausted
        """
        _ORDER_MAX_RETRIES: int = min(_MAX_RETRIES, 3)
        _ORDER_RETRY_DELAYS: list = [3.0, 10.0, 20.0]
        # Delays used specifically when the exchange is in cancel-only mode.
        # The disruption window is typically short (5–30s), so we wait longer
        # between retries to give the exchange time to resume normal operation.
        _MARKET_DISRUPTED_RETRY_DELAYS: list = [15.0, 30.0, 60.0]
        _RETRYABLE_KEYWORDS: tuple = (
            "timed out", "timeout", "connection", "read timed",
            "network", "connectionpool", "remotedisconnected",
            "connection reset", "broken pipe",
        )

        logger.info(
            "Placing order",
            product_id=product_id,
            size=size,
            side=side,
            order_type=order_type,
            limit_price=limit_price,
        )

        if isinstance(order_type, str):
            if order_type == "market_order":
                order_type_enum = OrderType.MARKET
            elif order_type == "limit_order":
                order_type_enum = OrderType.LIMIT
            else:
                order_type_enum = order_type
        else:
            order_type_enum = order_type

        last_exception: Optional[Exception] = None

        for attempt in range(_ORDER_MAX_RETRIES + 1):
            try:
                response = self._make_request(
                    self.client.place_order,
                    product_id=product_id,
                    size=size,
                    side=side,
                    order_type=order_type_enum,
                    limit_price=limit_price,
                    **kwargs,
                )
                if attempt > 0:
                    logger.info(
                        f"Order placed successfully on retry attempt {attempt + 1}",
                        order_id=response.get("id"),
                    )
                else:
                    logger.info("Order placed", order_id=response.get("id"))
                return cast(Dict[str, Any], response)

            except (APIError, Exception) as exc:
                last_exception = exc
                error_msg = str(exc).lower()

                # market_disrupted_cancel_only_mode is a transient exchange state
                # (settlement window, circuit breaker, etc.) — retry with longer delays.
                if "market_disrupted_cancel_only_mode" in error_msg:
                    if attempt >= _ORDER_MAX_RETRIES:
                        logger.error(
                            f"Order failed: exchange still in cancel-only mode after "
                            f"{_ORDER_MAX_RETRIES + 1} attempts. Last error: {exc}"
                        )
                        break
                    delay = _MARKET_DISRUPTED_RETRY_DELAYS[min(attempt, len(_MARKET_DISRUPTED_RETRY_DELAYS) - 1)]
                    logger.warning(
                        f"Exchange is in cancel-only mode (market_disrupted_cancel_only_mode). "
                        f"Attempt {attempt + 1}/{_ORDER_MAX_RETRIES + 1}. "
                        f"Waiting {delay:.0f}s for market to resume before retry..."
                    )
                    time.sleep(delay)
                    continue

                is_retryable = any(kw in error_msg for kw in _RETRYABLE_KEYWORDS)
                if not is_retryable:
                    logger.error(f"Order failed with non-retryable error: {exc}")
                    raise

                if attempt >= _ORDER_MAX_RETRIES:
                    logger.error(f"Order failed after {_ORDER_MAX_RETRIES + 1} attempts: {exc}")
                    break

                delay = _ORDER_RETRY_DELAYS[min(attempt, len(_ORDER_RETRY_DELAYS) - 1)]
                logger.warning(
                    f"Order attempt {attempt + 1} timed out. Waiting {delay:.0f}s before retry..."
                )
                time.sleep(delay)

        raise APIError(
            f"Order failed after {_ORDER_MAX_RETRIES + 1} attempts. Last error: {last_exception}"
        )

    def cancel_order(self, product_id: int, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        logger.info("Cancelling order", product_id=product_id, order_id=order_id)
        response = self._make_request(self.client.cancel_order, product_id, order_id)
        logger.info("Order cancelled", order_id=order_id)
        return cast(Dict[str, Any], response)

    def cancel_all_orders(self, product_id: int) -> Dict[str, Any]:
        """Cancel all open orders for a product."""
        logger.info("Cancelling all orders", product_id=product_id)
        response = self._make_request(self.client.cancel_all_orders, product_id)
        logger.info("All orders cancelled", product_id=product_id)
        return cast(Dict[str, Any], response)

    def set_leverage(self, product_id: int, leverage: str) -> Dict[str, Any]:
        """Set leverage for a product.

        Args:
            product_id: Product ID
            leverage: Leverage value (e.g., "200")

        Returns:
            Response
        """
        logger.info("Setting leverage", product_id=product_id, leverage=leverage)
        response = self._make_request(self.client.set_leverage, product_id, leverage)
        return cast(Dict[str, Any], response)

    def close_position(self, product_id: int) -> Dict[str, Any]:
        """Close an open position by placing an opposing market order.

        Args:
            product_id: Product ID

        Returns:
            Order response dict
        """
        # Get current position
        positions = self.get_positions(product_id=product_id)
        position = next(
            (p for p in positions if str(p.get("product_id")) == str(product_id)),
            None,
        )

        if position is None:
            logger.info(f"No open position for product {product_id}")
            return {"status": "no_position"}

        size = abs(int(float(position.get("size", 0))))
        if size == 0:
            logger.info(f"Position size is 0 for product {product_id}")
            return {"status": "no_position"}

        # Determine closing side
        current_size = float(position.get("size", 0))
        close_side = "buy" if current_size < 0 else "sell"

        logger.info(
            f"Closing position: product={product_id}, size={size}, side={close_side}"
        )

        return self.place_order(
            product_id=product_id,
            size=size,
            side=close_side,
            order_type="market_order",
        )

    def get_option_mark_price(self, product_id: int) -> float:
        """Get the current mark price for an option product.

        Args:
            product_id: Option product ID

        Returns:
            Current mark price
        """
        try:
            position = self.get_position(product_id)
            mark_price = float(position.get("mark_price", 0))
            if mark_price > 0:
                return mark_price
        except Exception:
            pass

        # Fallback: fetch from products list
        try:
            response = self._make_direct_request(f"/v2/products/{product_id}")
            result = response.get("result", {})
            return float(result.get("mark_price", 0))
        except Exception as e:
            logger.warning(f"Could not get mark price for product {product_id}: {e}")
            return 0.0

    def get_order(self, order_id: int) -> Dict[str, Any]:
        """Get order details by ID.

        Args:
            order_id: Order ID

        Returns:
            Order details dictionary
        """
        logger.debug("Fetching order", order_id=order_id)
        response = self._make_auth_request("GET", f"/v2/orders/{order_id}")
        return cast(Dict[str, Any], response.get('result', response))
