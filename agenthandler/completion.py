"""Completion claims and independently supplied acceptance evidence."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class CompletionStatus(str, Enum):
    RUNNING = "running"
    PROPOSED = "proposed"
    VERIFIED = "verified"
    INVALID = "invalid"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class VerificationResult:
    """Result from a trusted application validator, not an agent's done flag.

    Successful validation must include JSON-serializable evidence, such as
    artifact identifiers, test results, or receipts checked by the validator.
    """

    passed: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def validate(self) -> None:
        import json

        if type(self.passed) is not bool or not isinstance(self.evidence, dict):
            raise ValueError("Verification requires a boolean passed flag and evidence object")
        if not isinstance(self.reason, str):
            raise ValueError("Verification reason must be a string")
        if self.passed and not self.evidence:
            raise ValueError("Successful verification requires evidence")
        json.dumps(self.evidence, allow_nan=False)
