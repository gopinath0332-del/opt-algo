"""Rate limiter for API requests."""

import time
from collections import deque
from threading import Lock
from typing import Dict, Optional

from core.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """Token bucket rate limiter for API requests.

    Delta Exchange limits: 150 connections per 5 minutes per IP
    """

    def __init__(self, max_requests: int = 150, time_window: int = 300):
        """Initialize rate limiter.

        Args:
            max_requests: Maximum number of requests allowed
            time_window: Time window in seconds (default: 300 = 5 minutes)
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: deque = deque()
        self.lock = Lock()

        logger.info("Rate limiter initialized", max_requests=max_requests, time_window=time_window)

    def acquire(self, endpoint: Optional[str] = None) -> bool:
        """Acquire permission to make a request.

        Args:
            endpoint: API endpoint (for logging purposes)

        Returns:
            True if request is allowed, False otherwise
        """
        with self.lock:
            current_time = time.time()

            # Remove requests outside the time window
            while self.requests and self.requests[0] < current_time - self.time_window:
                self.requests.popleft()

            if len(self.requests) < self.max_requests:
                self.requests.append(current_time)
                return True
            else:
                oldest_request = self.requests[0]
                wait_time = self.time_window - (current_time - oldest_request)

                logger.error(
                    "Rate limit reached",
                    endpoint=endpoint,
                    wait_time=f"{wait_time:.2f}s",
                    requests_in_window=len(self.requests),
                )
                return False

    def wait_if_needed(self, endpoint: Optional[str] = None) -> None:
        """Wait if rate limit is reached.

        Args:
            endpoint: API endpoint (for logging purposes)
        """
        while not self.acquire(endpoint=endpoint):
            with self.lock:
                if not self.requests:
                    break
                current_time = time.time()
                oldest_request = self.requests[0]
                wait_time = max(0, self.time_window - (current_time - oldest_request)) + 1

            logger.info("Waiting for rate limit", endpoint=endpoint, wait_time=f"{wait_time:.2f}s")
            time.sleep(wait_time)

    def get_remaining_requests(self) -> int:
        """Get number of remaining requests in current window."""
        with self.lock:
            current_time = time.time()
            while self.requests and self.requests[0] < current_time - self.time_window:
                self.requests.popleft()
            return self.max_requests - len(self.requests)

    def reset(self) -> None:
        """Reset the rate limiter."""
        with self.lock:
            self.requests.clear()
            logger.info("Rate limiter reset")
