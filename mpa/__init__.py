"""playlist-dig / mpa - fetch a music playlist as data you can check.

The library never calls sys.exit and never prints to stdout on its own: it raises
typed errors (mpa.errors) and returns objects. See mpa/cli.py for the executable
surface.
"""

from .errors import (EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_ROUTE_UNAVAILABLE,
                     EXIT_UPSTREAM_CHANGED, EXIT_USAGE, FetchError, InputError,
                     MPError, RouteUnavailable, UpstreamChanged)
from .model import SCHEMA_PLAYLIST, Field, Playlist, Track
from .pipeline import fetch_playlist, load_dataset, verify_playlist

__version__ = "2.1.1"

__all__ = ["__version__", "SCHEMA_PLAYLIST", "Field", "Playlist",
           "Track", "fetch_playlist", "verify_playlist", "load_dataset",
           "MPError", "FetchError", "InputError", "UpstreamChanged",
           "RouteUnavailable", "EXIT_OK", "EXIT_PARTIAL", "EXIT_FAILED",
           "EXIT_UPSTREAM_CHANGED", "EXIT_USAGE", "EXIT_ROUTE_UNAVAILABLE"]
