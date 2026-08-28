"""Public KUMA Python SDK API.

Importing this module only binds types and functions; it does not read
credentials, inspect the environment, access the network, or create files.
"""

from ._version import __version__
from .api import configure, create_run
from .client import DEFAULT_BASE_URL, KumaClient
from .contracts import (
    CaptureComponent,
    CaptureStatus,
    Case,
    FileChange,
    FileEvidence,
    HistoryItem,
    JudgeBatchResult,
    KumaInput,
    Submission,
    TestReport,
)
from .errors import (
    AuthenticationError,
    CaseIntegrityError,
    ConfigurationError,
    DockerRequiredError,
    EvidenceCaptureError,
    InputProtocolError,
    LimitExceededError,
    PermissionDeniedError,
    ProviderError,
    RepoStateMismatchError,
    RunAlreadyActiveError,
    SensitiveDataError,
    ServiceBusyError,
    ServiceError,
    ValidationError,
)
from .exceptions import (
    KumaAPIError,
    KumaAuthenticationError,
    KumaError,
    KumaPermissionError,
    KumaRateLimitError,
    KumaTimeoutError,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "AuthenticationError",
    "CaptureComponent",
    "CaptureStatus",
    "Case",
    "CaseIntegrityError",
    "ConfigurationError",
    "DockerRequiredError",
    "EvidenceCaptureError",
    "FileChange",
    "FileEvidence",
    "HistoryItem",
    "InputProtocolError",
    "JudgeBatchResult",
    "KumaAPIError",
    "KumaAuthenticationError",
    "KumaClient",
    "KumaError",
    "KumaInput",
    "KumaPermissionError",
    "KumaRateLimitError",
    "KumaTimeoutError",
    "LimitExceededError",
    "PermissionDeniedError",
    "ProviderError",
    "RepoStateMismatchError",
    "RunAlreadyActiveError",
    "SensitiveDataError",
    "ServiceBusyError",
    "ServiceError",
    "Submission",
    "TestReport",
    "ValidationError",
    "__version__",
    "configure",
    "create_run",
]
