"""Validate coded public gaps and explain server-only optional model-body omission."""

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError
from ..evidence.runtime_contract import CASEGEN_EVIDENCE_CAPABILITY_ORDER

_BUDGET = "model_input_budget_omission"
_CODES = {_BUDGET, "required_evidence_missing", "evidence_contract_unavailable"}


def budget_gap_notice(gaps: list[Mapping[str, Any]], status: str) -> str | None:
    """Validate coded gaps and return only static SDK-owned display wording.

    Args:
        gaps: Mapping list already shape-checked by Official Judgment parsing.
            Legacy uncoded gap mappings retain their prior behavior. Coded gaps
            use exactly code/required_evidence, up to three unique known codes.
        status: Validated public overall status. Budget omission cannot coexist
            with pass/passed; malformed success is rejected, never rewritten.
    Returns:
        Fixed English notice when budget omission is present, otherwise None.
    Raises:
        ProviderError: Invalid coded gap, capability order, duplicate/unknown
            code or a contradictory passed result; safe invalid_response only.
    Preconditions:
        Official normalization has checked private fields and collection shapes.
    Postconditions:
        No source gap, verdict, Evidence, hash or signed Case is changed. Missing
        legacy gaps stay missing; no omission is inferred from local byte sizes.
    Side Effects:
        None. Called before report persistence and pending-operation completion.
    Security/Privacy:
        Does not interpolate remote text, expose private prompts, estimate model
        overhead, retry requests or equate omitted model bodies with Agent failure.
    """
    if not any("code" in gap for gap in gaps):
        return None
    codes: set[str] = set()
    valid = len(gaps) <= 3
    for gap in gaps:
        code, required = gap.get("code"), gap.get("required_evidence")
        if (
            set(gap) != {"code", "required_evidence"}
            or not isinstance(code, str)
            or code not in _CODES
            or code in codes
            or not isinstance(required, list)
        ):
            valid = False
            break
        codes.add(code)
        canonical = [x for x in CASEGEN_EVIDENCE_CAPABILITY_ORDER if x in required]
        if required != canonical or bool(required) != (
            code == "required_evidence_missing"
        ):
            valid = False
            break
    if not valid or (_BUDGET in codes and status in {"pass", "passed"}):
        raise ProviderError(
            "The Backend returned invalid Evidence gaps", code="invalid_response"
        )
    if _BUDGET not in codes:
        return None
    return (
        "The service omitted optional Trace model input/output bodies from the "
        "Judge's model view to fit its input budget. Original submitted Evidence "
        "and Case are unchanged. This limits review coverage; it does not establish "
        "an Agent failure. Mandatory content may still exceed the service limit."
    )
