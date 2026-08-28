"""Public provider contracts and adapters."""

from .base import (
    CallableCaseProvider,
    CallableJudgeProvider,
    CaseGenerationContext,
    CaseProvider,
    JudgeContext,
    JudgeProvider,
    adapt_case_provider,
    adapt_judge_provider,
)
from .normalization import normalize_case, normalize_report
from .official import OfficialCaseProvider, OfficialJudgeProvider

__all__ = [
    "CallableCaseProvider",
    "CallableJudgeProvider",
    "CaseGenerationContext",
    "CaseProvider",
    "JudgeContext",
    "JudgeProvider",
    "OfficialCaseProvider",
    "OfficialJudgeProvider",
    "adapt_case_provider",
    "adapt_judge_provider",
    "normalize_case",
    "normalize_report",
]
