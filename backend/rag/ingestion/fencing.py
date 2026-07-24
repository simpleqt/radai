"""Shared fencing primitives for pending-file ingestion."""


class IngestFenceLostError(RuntimeError):
    """Raised when a newer worker owns the pending-file write lease."""
