"""Required application-owned acceptance stages for any agent framework.

The gate invokes validators; it does not accept model claims or cached reports.
Callbacks are trusted application code and must independently inspect artifacts.
The owning application supplies persistent intent and restart coordination.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Dict, Mapping, Sequence, Tuple

from .completion import VerificationResult


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class VerificationStageResult:
    status: VerificationStatus
    evidence: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        # Freeze a detached JSON snapshot, including nested containers. Callers
        # may keep or mutate the validator's original evidence after this point.
        if not isinstance(self.evidence, Mapping):
            raise ValueError("Verification stage evidence must be a JSON object")
        object.__setattr__(self, "evidence", _freeze(_copy(_thaw(self.evidence))))


@dataclass(frozen=True)
class VerificationReport:
    """Evidence from one gate invocation, not a reusable authorization token."""

    required_stages: Tuple[str, ...]
    stages: Mapping[str, VerificationStageResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_stages", tuple(self.required_stages))
        object.__setattr__(self, "stages", MappingProxyType(dict(self.stages)))

    @property
    def status(self) -> VerificationStatus:
        results = [self.stages.get(name) for name in self.required_stages]
        if any(result and result.status == VerificationStatus.FAILED for result in results):
            return VerificationStatus.FAILED
        if results and all(
            result and result.status == VerificationStatus.VERIFIED for result in results
        ):
            return VerificationStatus.VERIFIED
        return VerificationStatus.UNVERIFIED

    @property
    def verified(self) -> bool:
        return self.status == VerificationStatus.VERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "required_stages": list(self.required_stages),
            "stages": {
                name: {
                    "status": result.status.value,
                    "evidence": _thaw(result.evidence),
                    "reason": result.reason,
                }
                for name, result in self.stages.items()
            },
        }

    def as_result(self) -> VerificationResult:
        """Adapt this gate to a verifier accepting VerificationResult."""
        return VerificationResult(self.verified, self.to_dict(), self.status.value)


@dataclass(frozen=True)
class VerificationGate:
    """Fail closed unless every named stage independently succeeds.

    ``timeout_seconds`` is a total deadline across all validators. Async
    cancellation propagates. Callbacks must cooperate with cancellation and
    clean up subprocesses; this class cannot kill an arbitrary Python callable.
    Each invocation starts fresh and each validator receives a detached JSON
    context. Persist artifact identity with evidence in the owning application.
    """

    required_stages: Sequence[str]

    def __post_init__(self) -> None:
        if isinstance(self.required_stages, (str, bytes)):
            raise ValueError("Required stages must be a sequence of unique names")
        names = tuple(self.required_stages)
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("Required stages must have nonempty string names")
        if len(set(names)) != len(names):
            raise ValueError("Required stages must have unique names")
        object.__setattr__(self, "required_stages", names)

    async def run(
        self,
        context: Dict[str, Any],
        *,
        validators: Mapping[str, Callable[[Dict[str, Any]], Awaitable[VerificationResult]]],
        timeout_seconds: float = 120,
    ) -> VerificationReport:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Verification timeout must be a finite positive number")
        if not isinstance(context, dict):
            raise ValueError("Verification context must be a JSON object")
        safe_context = _copy(context)
        callbacks = dict(validators)
        names = tuple(self.required_stages)
        stages: Dict[str, VerificationStageResult] = {}
        # Missing registration invalidates the whole contract before any checks run.
        missing = [name for name in names if name not in callbacks]
        if missing:
            for name in names:
                reason = (
                    "Required validator is not registered"
                    if name in missing
                    else "Not run: required validators are missing"
                )
                stages[name] = VerificationStageResult(VerificationStatus.UNVERIFIED, reason=reason)
            return VerificationReport(names, stages)
        deadline = time.monotonic() + timeout_seconds
        for name in names:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                result = await asyncio.wait_for(callbacks[name](_copy(safe_context)), remaining)
                # A callback that suppresses cancellation cannot turn timeout into success.
                if time.monotonic() > deadline:
                    raise asyncio.TimeoutError()
                if not isinstance(result, VerificationResult):
                    raise ValueError("Validator must return VerificationResult")
                result.validate()
                stages[name] = VerificationStageResult(
                    VerificationStatus.VERIFIED if result.passed else VerificationStatus.FAILED,
                    _copy(result.evidence),
                    result.reason,
                )
            except asyncio.TimeoutError:
                stages[name] = VerificationStageResult(
                    VerificationStatus.FAILED, reason="Verification deadline exceeded"
                )
            except Exception as exc:
                stages[name] = VerificationStageResult(
                    VerificationStatus.FAILED, reason=f"{type(exc).__name__}: {exc}"
                )
            if stages[name].status != VerificationStatus.VERIFIED:
                # No dependent validator runs after a rejection or exception.
                for skipped in names[len(stages) :]:
                    stages[skipped] = VerificationStageResult(
                        VerificationStatus.UNVERIFIED, reason="Not run: an earlier stage failed"
                    )
                break
        return VerificationReport(names, stages)
