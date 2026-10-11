"""Error taxonomy and process exit codes.

Design rule: library code never calls sys.exit(). It raises an MPError subclass;
only mpa/cli.py turns an exception into an exit code. That is what makes partial
success, retries and offline re-derivation possible.
"""

from __future__ import annotations

# --- error kinds ------------------------------------------------------------

KIND_INPUT = "input"                    # bad argument / unparsable URL
KIND_NETWORK = "network"                # DNS, TCP, TLS, timeout
KIND_HTTP = "http"                      # non-2xx status (other than blocked/notfound)
KIND_BLOCKED = "blocked"                # 401/402/403 - upstream refuses us
KIND_NOTFOUND = "notfound"              # 404 / playlist does not exist
KIND_PRIVATE = "private"                # playlist exists but is not readable
KIND_PARSE = "parse"                    # response was not what we expect
KIND_UPSTREAM_CHANGED = "upstream_changed"   # response shape drifted
KIND_TRUNCATED = "truncated"            # upstream caps the list below declared
KIND_ROUTE_UNAVAILABLE = "route_unavailable"  # needs node/npm or a service we refuse to run

RETRYABLE = {KIND_NETWORK, KIND_HTTP}

# --- exit codes (documented in README and `mpa --help`) ---------------------

EXIT_OK = 0                  # complete: nothing declared was lost
EXIT_PARTIAL = 1             # usable data, but ledger has a gap (see warnings)
EXIT_FAILED = 2              # could not produce data
EXIT_UPSTREAM_CHANGED = 3    # produced data, but a contract check failed
EXIT_USAGE = 4               # bad invocation
EXIT_ROUTE_UNAVAILABLE = 5   # this route needs node/npm; see docs


class MPError(Exception):
    """Base class for every error this package raises on purpose."""

    kind = KIND_INPUT

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message)
        self.message = message
        self.context = context or {}

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def to_json(self) -> dict:
        return {"kind": self.kind, "message": self.message, "context": self.context}


class InputError(MPError):
    kind = KIND_INPUT


class FetchError(MPError):
    """Anything that went wrong while talking to a provider."""

    kind = KIND_NETWORK

    def __init__(self, message: str, *, kind: str | None = None,
                 context: dict | None = None):
        super().__init__(message, context=context)
        if kind:
            self.kind = kind


class UpstreamChanged(FetchError):
    kind = KIND_UPSTREAM_CHANGED


class RouteUnavailable(FetchError):
    kind = KIND_ROUTE_UNAVAILABLE


class DatasetError(MPError):
    """The dataset on disk is not readable: a raw part or the manifest is gone.

    Raised when re-reading a stored dataset, never while fetching. ``MPError``
    (not ``FetchError``) on purpose: nothing was fetched, so there is no retry
    hint to give - the fix is to re-run ``mpa fetch`` into the same directory.
    """

    kind = KIND_PARSE


def exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, InputError):
        return EXIT_USAGE
    if isinstance(exc, RouteUnavailable):
        return EXIT_ROUTE_UNAVAILABLE
    if isinstance(exc, UpstreamChanged):
        return EXIT_UPSTREAM_CHANGED
    if isinstance(exc, MPError):
        return EXIT_FAILED
    return EXIT_FAILED
