"""Internal evidence tracking adapters."""

from .diff import DiffResult, compare_snapshots
from .evidence import EvidenceCollector, PreparedEvidence
from .logs import LogState, LogTracker, PreparedLogs
from .snapshot import Snapshot, SnapshotEntry, Snapshotter

__all__ = [
    "DiffResult",
    "EvidenceCollector",
    "LogState",
    "LogTracker",
    "PreparedEvidence",
    "PreparedLogs",
    "Snapshot",
    "SnapshotEntry",
    "Snapshotter",
    "compare_snapshots",
]
