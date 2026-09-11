"""Credential-isolated GitHub Release checks, independent of paid SDK transport."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ._version import __version__

_RELEASE_API = "https://api.github.com/repos/DefuzeX-AI/KUMA-DefuzeX/releases/latest"
_RELEASE_PAGE = "https://github.com/DefuzeX-AI/KUMA-DefuzeX/releases/tag/v"
_VERSION = re.compile(
    r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z"
)
_CACHE_SECONDS = 24 * 60 * 60
_MAX_BYTES = 65536
_LOCK = threading.Lock()
_cached: dict[str, str | bool | None] | None = None
_expires = 0.0
_inflight = False


class _NoRedirect(HTTPRedirectHandler):
    """Keep the anonymous updater on its fixed official HTTPS endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Decline every redirect; urllib closes the response and raises HTTPError."""
        return None


def _disabled() -> bool:
    """Read the opt-out at call time, including before cached results or I/O."""
    return os.environ.get("KUMA_DISABLE_UPDATE_CHECK") == "1"


def _result(status: str, latest: str | None = None) -> dict[str, str | bool | None]:
    """Build a detached, body-free result from a local status and validated version."""
    return {
        "status": status,
        "current_version": __version__,
        "latest_version": latest,
        "release_url": None if latest is None else _RELEASE_PAGE + latest,
        "cached": False,
    }


def _release_result(raw: bytes) -> dict[str, str | bool | None]:
    """Project bounded GitHub metadata into a safe semantic-version comparison.

    Only stable ``vX.Y.Z`` releases with exact false draft/prerelease flags are
    eligible. Arbitrary release text/URLs never enter results or terminal output.
    Malformed metadata raises ValueError/UnicodeError for the caller to suppress.
    A higher major/minor is required; patch-only is optional, never a hard block.
    """
    if len(raw) > _MAX_BYTES:
        raise ValueError("release response exceeds limit")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid release")
    tag = payload.get("tag_name")
    current_match = _VERSION.fullmatch(__version__)
    if (
        payload.get("draft") is not False
        or payload.get("prerelease") is not False
        or not isinstance(tag, str)
        or not tag.startswith("v")
        or not _VERSION.fullmatch(tag[1:])
        or current_match is None
    ):
        raise ValueError("invalid release version")
    latest = tuple(map(int, tag[1:].split(".")))
    current = tuple(map(int, current_match.groups()))
    status = "up_to_date"
    if latest > current:
        status = "required" if latest[:2] > current[:2] else "optional"
    return _result(status, tag[1:])


def _fetch_release() -> dict[str, str | bool | None]:
    """Fetch at most 64 KiB from GitHub without user credentials or redirects.

    Uses a private opener, no proxy/environment authentication, a one-second
    socket timeout and no retries. All response resources close before return.
    HTTP/DNS/TLS/JSON failures propagate only to the best-effort wrapper.
    """
    request = Request(
        _RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "kuma-updates"},
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=1.0) as response:
            if response.status != 200:
                raise ValueError("release unavailable")
            raw = response.read(_MAX_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise ValueError("release unavailable") from None
    return _release_result(raw)


def _reserve() -> dict[str, str | bool | None] | None:
    """Return cached/checking status or reserve the sole process-local fetch.

    The lock protects only metadata, never network I/O. None grants ownership;
    the caller must finish via _complete even if fetching or starting a thread
    fails. Successes and failures share the same 24-hour monotonic expiry.
    """
    global _inflight
    if not _LOCK.acquire(blocking=False):
        return _result("checking")
    try:
        if _cached is not None and time.monotonic() < _expires:
            return {**_cached, "cached": True}
        if _inflight:
            return _result("checking")
        _inflight = True
    finally:
        _LOCK.release()
    return None


def _complete(result: dict[str, str | bool | None]) -> None:
    """Publish one detached result and release fetch ownership without disk writes."""
    global _cached, _expires, _inflight
    with _LOCK:
        _cached = dict(result)
        _expires = time.monotonic() + _CACHE_SECONDS
        _inflight = False


def _perform() -> dict[str, str | bool | None]:
    """Complete a reserved attempt; suppress all ordinary update failures.

    Recheck opt-out immediately before I/O. No raw exception, remote body or
    host data is exposed. This independent read cannot retry or mutate a paid
    operation; the cache always releases its reservation after normal failure.
    """
    try:
        result = _result("disabled") if _disabled() else _fetch_release()
    except Exception:
        result = _result("unavailable")
    _complete(result)
    return result


def check_for_updates() -> dict[str, str | bool | None]:
    """Check the latest official GitHub Release without changing the installation.

    Args:
        None. Set KUMA_DISABLE_UPDATE_CHECK=1 to prevent even an explicit check.

    Returns:
        Detached dict with status (disabled, checking, unavailable, up_to_date,
        optional, required), current_version, latest_version (str or None),
        release_url (official URL or None) and cached (bool). A concurrent fetch
        returns checking immediately. Patch-only updates are optional; higher
        major/minor versions are required reminders, not business restrictions.

    Raises:
        No ordinary network/parse failure; unavailable is returned instead.

    Side Effects:
        At most one anonymous HTTPS request per process per 24 hours, including
        failures, with one-second socket timeout, 64 KiB limit and no retries.
        This explicit API may wait for I/O; automatic checks use a daemon thread.
        No files, credentials, subprocesses, pip or Backend calls are involved.

    Security/Privacy:
        No Agent/request/key data is sent. Redirects and proxies are disabled;
        only validated version numbers enter the result. Import performs no I/O.
    """
    if _disabled():
        return _result("disabled")
    existing = _reserve()
    return _perform() if existing is None else existing


def _background_check() -> None:
    """Emit at most one advisory after a reserved background fetch completes.

    Writes safe required/optional text to stderr only. Disabled/unavailable or
    current releases stay silent; terminal failures never affect SDK work. A
    short-lived process may exit before its daemon produces any reminder.
    """
    result = _perform()
    if _disabled() or result["status"] not in {"optional", "required"}:
        return
    try:
        label = (
            "需要更新 / Update required"
            if result["status"] == "required"
            else "可选更新 / Update optional"
        )
        print(
            f"KUMA: {label}: {result['current_version']} -> "
            f"{result['latest_version']}. {result['release_url']} "
            "(Reminder only; current work continues. 不会自动安装或中断任务。)",
            file=sys.stderr,
        )
    except Exception:
        pass


def schedule_update_check() -> None:
    """Schedule a best-effort check after real official transport succeeds.

    Called centrally by Backend transport for Python and CLI official workflows,
    never by imports, local/custom providers or parser/help. Returns without
    waiting for network I/O. One process-local cache/reservation deduplicates
    simultaneous requests and suppresses retries for 24 hours, including failure.
    Thread startup/terminal/network failures cannot change the original result.
    """
    if _disabled() or _reserve() is not None:
        return
    try:
        threading.Thread(
            target=_background_check, name="kuma-updates", daemon=True
        ).start()
    except Exception:
        _complete(_result("unavailable"))
