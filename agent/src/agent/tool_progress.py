"""Per-run tool observations, independent of the activity watchdog."""

from __future__ import annotations

import hashlib
import json

NO_PROGRESS_LIMIT = 8
RECOVERY_MESSAGE = (
    "I stopped because repeated tool attempts did not produce new information. "
    "I cannot reliably answer from the previous summary alone. Please provide "
    "the artifact path or confirm that you want to rerun the missing research step."
)


class ToolProgress:
    """Bound repeated discovery without treating activity as new evidence."""

    def __init__(self) -> None:
        self.failed: set[tuple[str, str]] = set()
        self._observations: set[tuple[str, str | None, str]] = set()
        self._new_observation = False
        self.stalled_iterations = 0

    def record(
        self,
        name: str,
        key: tuple[str, str] | None,
        result: str,
        *,
        success: bool,
        is_readonly: bool = True,
    ) -> None:
        """Record an executed call; synthetic skips must not enter this ledger."""
        if not success:
            if key is not None:
                self.failed.add(key)
            return
        if not is_readonly:
            self.failed.clear()
        if not is_readonly and key is None:
            self._new_observation = True
            return
        try:
            result = json.dumps(json.loads(result), sort_keys=True, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        observation = (
            name,
            None if is_readonly else key[1],
            hashlib.sha256(result.encode("utf-8")).hexdigest(),
        )
        if observation not in self._observations:
            self._observations.add(observation)
            self._new_observation = True

    def finish_iteration(self) -> bool:
        """Return whether the run exhausted its consecutive no-progress budget."""
        self.stalled_iterations = (
            0 if self._new_observation else self.stalled_iterations + 1
        )
        self._new_observation = False
        return self.stalled_iterations >= NO_PROGRESS_LIMIT
