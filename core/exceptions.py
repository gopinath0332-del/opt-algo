"""Custom exceptions for the Delta Exchange Options trading platform."""

from typing import Optional


class DeltaExchangeError(Exception):
    """Base exception for all Delta Exchange related errors."""

    pass


class APIError(DeltaExchangeError):
    """Exception raised for API-related errors."""

    def __init__(
        self, message: str, status_code: Optional[int] = None, response: Optional[dict] = None
    ):
        """Initialize APIError.

        Args:
            message: Error message
            status_code: HTTP status code if applicable
            response: API response dictionary if available
        """
        self.message = message
        self.status_code = status_code
        self.response = response
        super().__init__(self.message)


class AuthenticationError(APIError):
    """Exception raised for authentication failures."""

    pass


class RateLimitError(APIError):
    """Exception raised when API rate limit is exceeded."""

    pass


class DataError(DeltaExchangeError):
    """Exception raised for data-related errors."""

    pass


class ValidationError(DeltaExchangeError):
    """Exception raised for validation errors."""

    pass


class TradingError(DeltaExchangeError):
    """Exception raised for trading-related errors."""

    def __init__(self, message: str, order_id: Optional[str] = None):
        """Initialize TradingError.

        Args:
            message: Error message
            order_id: Order ID if applicable
        """
        self.message = message
        self.order_id = order_id
        super().__init__(self.message)


class InsufficientFundsError(TradingError):
    """Exception raised when account has insufficient funds."""

    pass


class InvalidOrderError(TradingError):
    """Exception raised for invalid order parameters."""

    pass


class StrategyError(DeltaExchangeError):
    """Exception raised for strategy-related errors."""

    pass
