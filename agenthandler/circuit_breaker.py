"""Circuit breaker — stops calling tools that keep failing."""

import threading
import time
from enum import Enum
from typing import Optional

from .errors import AgentHandlerError


class CircuitState(Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking calls (too many failures)
    HALF_OPEN = "half_open"  # Allowing one test call


class CircuitBreaker:
    """Per-tool circuit breaker. Thread-safe.

    States:
        CLOSED  → failures < threshold, calls pass through
        OPEN    → failures >= threshold, calls blocked until reset_after
        HALF_OPEN → reset_after elapsed, allow one test call

    Usage:
        cb = CircuitBreaker(threshold=3, reset_after=60.0)
        cb.check("web_search")      # raises if circuit open
        try:
            result = call_tool()
            cb.record_success()
        except:
            cb.record_failure()
    """

    def __init__(self, threshold: int = 5, reset_after: float = 60.0):
        self._threshold = threshold
        self._reset_after = reset_after
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()
        self._probe_in_flight = False
        self._generation = 0

    def check(self, tool_name: str) -> int:
        """Reserve a call and return its generation; allow only one recovery probe."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if (
                    self._opened_at is not None
                    and time.monotonic() - self._opened_at >= self._reset_after
                ):
                    self._state = CircuitState.HALF_OPEN
                else:
                    raise AgentHandlerError.circuit_open(tool_name, self._consecutive_failures)
            if self._state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise AgentHandlerError.circuit_open(tool_name, self._consecutive_failures)
                self._probe_in_flight = True
            return self._generation

    def record_success(self, generation: Optional[int] = None) -> None:
        """Record a success, ignoring calls admitted before the latest trip."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self, generation: Optional[int] = None) -> None:
        """Record a failure, reopening after a failed or cancelled probe."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._consecutive_failures += 1
            if (
                self._state == CircuitState.HALF_OPEN
                or self._consecutive_failures >= self._threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._probe_in_flight = False
                self._generation += 1

    @property
    def state(self) -> CircuitState:
        with self._lock:
            # Check for auto-transition to half-open
            if self._state == CircuitState.OPEN and self._opened_at is not None:
                if (time.monotonic() - self._opened_at) >= self._reset_after:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures
