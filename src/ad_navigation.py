"""Bound retries for a stalled viewer without timing or altering its ads."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math


DEFAULT_AD_RETRIES = 10
DEFAULT_AD_RETRY_COOLDOWN = 5.0
DEFAULT_AD_LOAD_TIMEOUT = 30.0


def validate_ad_retry_settings(retries, cooldown) -> tuple[int, float]:
    """Normalize settings from the GUI, command line or saved configuration."""
    if isinstance(retries, bool) or isinstance(cooldown, bool):
        raise ValueError("Retries and cooldown must be numbers, not booleans.")
    try:
        number = Decimal(str(retries))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError
        normalized_retries = int(number)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise ValueError("Retries must be a nonnegative whole number.") from None
    try:
        normalized_cooldown = float(cooldown)
        if not math.isfinite(normalized_cooldown) or normalized_cooldown < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Retry cooldown must be a finite, nonnegative number of seconds.") from None
    return normalized_retries, normalized_cooldown


class ViewerNavigationWatchdog:
    def __init__(
        self, now: float, load_timeout: float = DEFAULT_AD_LOAD_TIMEOUT,
        retry_delay: float = DEFAULT_AD_RETRY_COOLDOWN, max_retries: int = DEFAULT_AD_RETRIES,
    ):
        max_retries, retry_delay = validate_ad_retry_settings(max_retries, retry_delay)
        if not math.isfinite(load_timeout) or load_timeout <= 0:
            raise ValueError("Navigation load timeout must be finite and positive.")
        self.load_timeout = load_timeout
        self.retry_delay = retry_delay
        self.max_retries = max_retries
        self.attempts = 0
        self._loading_since = now
        self._fault_since = None

    def observe(self, state: str, now: float) -> str | None:
        """Return a navigation action; healthy ads have no timeout here."""
        if state in ("ad", "ticket", "interactive"):
            self._loading_since = None
            self._fault_since = None
            return None

        if state == "loading":
            self._fault_since = None
            if self._loading_since is None:
                self._loading_since = now
            due = now - self._loading_since >= self.load_timeout + self.retry_delay
        elif state in ("error", "neutral"):
            if self._fault_since is None:
                self._fault_since = now
            due = now - self._fault_since >= self.retry_delay
        else:
            raise ValueError(f"Unknown viewer navigation state: {state}")

        if not due:
            return None
        if self.attempts >= self.max_retries:
            return "failed"
        self.attempts += 1
        self._loading_since = now
        self._fault_since = None
        return "retry"
