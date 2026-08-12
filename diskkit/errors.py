"""Error types for diskkit."""


class DiskKitError(Exception):
    """Raised for any recoverable failure in a diskkit operation.

    All public functions raise this (and only this) on failure so callers --
    including the CLI and the GUI -- have a single exception to catch.  Anything
    that is *not* a DiskKitError should be treated as a genuine bug.
    """
