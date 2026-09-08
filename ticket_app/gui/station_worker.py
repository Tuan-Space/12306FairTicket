"""Explicit, non-blocking station-list refresh support for the desktop GUI.

Nothing in this module starts a request on import or construction.  The UI
creates a worker only after the user presses ``更新站点`` and receives its
result through a small callback, making the network boundary straightforward
to test and to keep out of the ticket-running thread.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

import requests

from ticket_app.configuration import AppError, STATION_URL
from ticket_app.stations import StationStore

from .compat import STATION_CACHE_FILE


StationFetcher = Callable[[str, float], str]
RefreshCallback = Callable[[Optional[Dict[str, str]], Optional[Exception]], None]


def _atomic_write_station_cache(path: Path, stations: Mapping[str, str]) -> None:
    """Write a StationStore-compatible cache without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    document = {"fetched_at": time.time(), "stations": dict(stations)}
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _requests_fetcher(url: str, timeout: float) -> str:
    """The only default network operation, invoked exclusively by refresh."""

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def refresh_station_cache(
    *,
    cache_path: Path = STATION_CACHE_FILE,
    timeout_seconds: float = 10.0,
    fetcher: StationFetcher = _requests_fetcher,
) -> Dict[str, str]:
    """Fetch, parse and atomically cache the official station list.

    ``fetcher`` is injectable so tests never need a real 12306 request.  A
    failed download intentionally does not modify the existing cache.
    """

    if timeout_seconds <= 0:
        raise AppError("站点更新超时时间必须大于 0")
    try:
        payload = fetcher(STATION_URL, timeout_seconds)
        stations = StationStore._parse_station_js(payload)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(f"更新站点失败: {exc}") from exc
    _atomic_write_station_cache(cache_path, stations)
    return stations


class StationRefreshWorker(threading.Thread):
    """One-shot daemon worker for the ``更新站点`` button.

    The callback is executed on this worker thread.  A Qt caller should relay
    it through a signal before touching widgets; keeping Qt out of this data
    layer also makes the worker fully offline-testable.
    """

    def __init__(
        self,
        *,
        cache_path: Path = STATION_CACHE_FILE,
        timeout_seconds: float = 10.0,
        fetcher: StationFetcher = _requests_fetcher,
        callback: Optional[RefreshCallback] = None,
    ) -> None:
        super().__init__(name="station-refresh", daemon=True)
        self.cache_path = cache_path
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher
        self.callback = callback
        self.stations: Optional[Dict[str, str]] = None
        self.error: Optional[Exception] = None

    def run(self) -> None:
        try:
            self.stations = refresh_station_cache(
                cache_path=self.cache_path,
                timeout_seconds=self.timeout_seconds,
                fetcher=self.fetcher,
            )
        except Exception as exc:  # retained for the UI to report cleanly
            self.error = exc
        if self.callback:
            try:
                self.callback(self.stations, self.error)
            except RuntimeError:
                # The owning Qt window may have closed while a bounded network
                # request was still finishing. The daemon worker must not keep
                # or resurrect UI state in that case.
                pass
