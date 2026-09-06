"""Bound retries for a stalled viewer without timing or altering its ads."""
from __future__ import annotations


class ViewerNavigationWatchdog:
    def __init__(
        self, now: float, load_timeout: float = 30.0,
        retry_delay: float = 5.0, max_retries: int = 2,
    ):
        if load_timeout <= 0 or retry_delay < 0:
            raise ValueError("Navigation timeouts must be positive and retry delay nonnegative.")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("Maximum retries must be a nonnegative integer.")
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
            due = now - self._loading_since >= self.load_timeout
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
