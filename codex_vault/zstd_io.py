"""Optional Python 3.14+ Zstandard stream support.

SessionCrate deliberately has no third-party runtime dependencies.  Keeping
the optional import here lets older supported Python versions verify and
restore archived ``.zst`` source bytes without needing to decode them.
"""

from __future__ import annotations

from typing import Any

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None


class ZstdUnavailableError(RuntimeError):
    """The standard-library Zstandard decoder is unavailable."""


def is_available() -> bool:
    return _zstd is not None


def open_reader(stream: Any) -> Any:
    """Return a bounded-window reader that accepts concatenated frames."""

    if _zstd is None:
        raise ZstdUnavailableError("Zstandard content requires Python 3.14+")
    return _zstd.ZstdFile(
        stream,
        mode="rb",
        options={_zstd.DecompressionParameter.window_log_max: 27},
    )


def decode_errors() -> tuple[type[BaseException], ...]:
    """Exceptions raised for malformed, truncated, or trailing Zstd data."""

    if _zstd is None:
        return (EOFError,)
    return (_zstd.ZstdError, EOFError)


__all__ = ["ZstdUnavailableError", "decode_errors", "is_available", "open_reader"]
