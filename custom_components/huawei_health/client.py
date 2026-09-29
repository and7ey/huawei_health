"""Sync client for the Huawei Health app cloud.

Nothing here touches Home Assistant: the whole module is stdlib and blocking, so the
coordinator can run it in an executor job. It is a direct port of the protocol recovered
from Huawei Health 16.1.6.320 and verified against the live RU cloud, including the traps
that cost the most time to find:

* the token is a JSON *body* field, and the app also sends it in ``Authorization`` and
  ``x-token`` - the cloud accepts the pair only when it is presented that way;
* ``*Stat`` endpoints take YYYYMMDD integers while ``*ByTime`` ones take the same or epoch
  milliseconds, and epoch *seconds* are rejected everywhere;
* every ``*ByTime`` window is capped at ten calendar days.
"""

from __future__ import annotations

import gzip
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Iterator

from .const import (
    APP_HOSTS,
    APP_ID,
    APP_VERSION,
    AUTH_FAILED_CODES,
    AUTHORIZE_PATH,
    BAD_HUID_CODE,
    BY_TIME_WINDOW_DAYS,
    CODE_INVALID_CODE,
    HMS_APP_ID,
    OAUTH_BASE,
    OBTAIN_PATH,
    QUERY_PATH,
    READ_PATHS,
    REDIRECT_URI,
    REFRESH_PATH,
    REFRESH_TOKEN_TTL,
    RT_INVALID_CODE,
    SCOPES,
    SESSION_HOSTS,
    SESSION_SPORT_TYPES,
    TOKEN_LIVE_SLACK,
    TOKEN_TYPE,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Linux; Android 12) huawei-health-ha/1.0"


class HuaweiError(Exception):
    """Base error for the client."""


class HuaweiConnectionError(HuaweiError):
    """The cloud could not be reached - retry later."""


class HuaweiApiError(HuaweiError):
    """The cloud answered with a non-zero resultCode."""

    def __init__(self, code: Any, url: str, message: str = "") -> None:
        super().__init__(f"{url}: resultCode {code} - {message}")
        self.code = code
        self.url = url


class HuaweiAuthError(HuaweiError):
    """The stored credentials are gone for good and the account must log in again."""


# ── small tolerant parsers ───────────────────────────────────────────────


def to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    got = to_float(value)
    return None if got is None else int(got)


def normalize_day(raw: Any) -> date | None:
    """Huawei mixes YYYYMMDD ints, ISO strings and ms/ns epochs across endpoints."""
    if isinstance(raw, str) and "-" in raw:
        digits = "".join(ch for ch in raw if ch.isdigit())
        return _from_ymd(digits[:8]) if len(digits) >= 8 else None
    value = to_float(raw)
    if value is None:
        return None
    text = str(int(value))
    if len(text) == 8:
        return _from_ymd(text)
    if len(text) == 13:
        return datetime.fromtimestamp(value / 1000).date()
    if len(text) == 10:
        return datetime.fromtimestamp(value).date()
    if len(text) == 19:
        return datetime.fromtimestamp(value / 1e9).date()
    return None


def _from_ymd(digits: str) -> date | None:
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def day_int(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


def day_windows(start: int, end: int,
                window: int = BY_TIME_WINDOW_DAYS) -> Iterator[tuple[int, int]]:
    """Split a YYYYMMDD range into inclusive sub-ranges the *ByTime endpoints accept."""
    first = _from_ymd(str(start))
    last = _from_ymd(str(end))
    if not first or not last:
        return
    while first <= last:
        stop = min(first + timedelta(days=window - 1), last)
        yield day_int(first), day_int(stop)
        first = stop + timedelta(days=1)


# ── token state ──────────────────────────────────────────────────────────


@dataclass
class Tokens:
    """The pair plus the identity it was minted for.

    ``on_change`` exists because rotating invalidates the previous access token: the new
    pair has to reach durable storage before anything else can speak for the account.
    """

    access_token: str | None = None
    refresh_token: str | None = None
    uid: str | None = None
    site_id: Any = None
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    session_host: str | None = None
    on_change: Callable[[Tokens], None] | None = field(default=None, repr=False)
    dirty: bool = False

    @property
    def refresh_dead(self) -> bool:
        return bool(self.refresh_expires_at and time.time() > self.refresh_expires_at)

    def needs_rotation(self) -> bool:
        if not self.refresh_token or self.refresh_dead:
            return False
        return not self.access_token or time.time() > self.expires_at - TOKEN_LIVE_SLACK

    def apply(self, response: dict) -> Tokens:
        """Take an obtain/refresh answer (a ThirdPartyLoginInfo) into this state."""
        access = response.get("accessToken")
        if not access:
            raise HuaweiAuthError(f"no accessToken in {str(response)[:200]}")
        self.access_token = str(access)
        expire_ms = to_float(response.get("accessTokenExpireTime"))
        # accessTokenExpireTime is an epoch-milliseconds stamp, numeric string included.
        self.expires_at = (expire_ms / 1000 if expire_ms
                           else time.time() + 1800)
        new_refresh = response.get("refreshToken")
        # Verified live: rotation hands back the *same* refresh token, so its 180 days keep
        # running from the login and must not slide on rotation.
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = str(new_refresh)
            self.refresh_expires_at = time.time() + REFRESH_TOKEN_TTL
        elif not self.refresh_expires_at:
            self.refresh_expires_at = time.time() + REFRESH_TOKEN_TTL
        if response.get("uid"):
            self.uid = str(response["uid"])
        if response.get("siteId") is not None:
            self.site_id = response["siteId"]
        self.dirty = True
        if self.on_change:
            self.on_change(self)
        return self

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "uid": self.uid,
            "site_id": self.site_id,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "session_host": self.session_host,
        }


# ── login / token endpoints ──────────────────────────────────────────────


def authorization_url(*, client_id: str = HMS_APP_ID, scopes: Iterable[str] = SCOPES,
                      language: str = "en") -> str:
    """The page a human finishes in a browser.

    ``hms://redirect_url`` is the only redirect registered for this client, so the browser
    ends on a link nothing can open and the code has to be read out of that address.
    """
    query = urllib.parse.urlencode({
        "client_id": str(client_id),
        "response_type": "code",
        "access_type": "offline",
        "display": "touch",
        "scope": " ".join(scopes),
        "redirect_uri": REDIRECT_URI,
        "state": uuid.uuid4().hex[:8],
        "lang": language,
        "prompt": "consent",
    })
    return f"{OAUTH_BASE}{AUTHORIZE_PATH}?{query}"


def authorization_code_from(answer: str) -> str | None:
    """Pull the code out of whatever the human pasted.

    A full ``hms://redirect_url?code=..`` address, Chrome's
    ``Failed to launch 'hms://redirect_url?code=..' because the scheme does not have a
    registered handler.`` console line or the bare code all read the same way. The code is
    URL-escaped and ~360 chars long, so only a query parse is safe - slicing the address or
    unquoting it whole corrupts it.
    """
    text = (answer or "").strip().strip('"').strip("'")
    if not text:
        return None
    query = urllib.parse.urlparse(text).query
    if query:
        found = urllib.parse.parse_qs(query).get("code")
        if found and found[0]:
            return found[0]
    if "code=" in text:
        tail = text.split("code=", 1)[1].split("&", 1)[0].split("'")[0]
        if tail:
            return urllib.parse.unquote(tail)
    if "://" in text or text.startswith("Failed to launch"):
        return None
    if any(char.isspace() for char in text):
        return None
    # A bare code, still percent-escaped: it came out of an address bar, not a JSON body.
    return urllib.parse.unquote(text)


def _session_headers(tokens: Tokens, uid: str | None = None) -> dict[str, str]:
    """ThirdPartyHttpUtils.getHeader(): x-ts + x-version, and x-huid when it is known."""
    headers = {
        "x-ts": str(int(time.time() * 1000)),
        "x-version": APP_VERSION,
        "Content-Type": "application/json",
    }
    if uid:
        headers["x-huid"] = str(uid)
    return headers


def exchange_authorization_code(
        code: str, *, client_id: str = HMS_APP_ID,
        session_host: str | None = None, uid: str | None = None,
        timeout: int = 30) -> Tokens:
    """Trade a browser-captured authorization code for the token pair.

    The code is single-use: a refused one cannot be replayed, so a failed exchange means
    the flow has to start over in the browser.
    """
    tokens = Tokens(refresh_expires_at=time.time() + REFRESH_TOKEN_TTL,
                    session_host=session_host or SESSION_HOSTS[0])
    url = f"{tokens.session_host}{OBTAIN_PATH}"
    body = {"authorizationCode": code, "appId": str(client_id)}
    # A stored x-huid can belong to an account logged in earlier on this machine, so the
    # first answer of 20010004 is retried without the header rather than reported.
    response = _json_request("POST", url, headers=_session_headers(tokens, uid),
                             body=body, timeout=timeout, raw=True)
    if _result_code(response) == BAD_HUID_CODE and uid:
        response = _json_request("POST", url, headers=_session_headers(tokens, None),
                                 body=body, timeout=timeout, raw=True)
    ensure_ok(response, url)
    return tokens.apply(response)


def refresh_tokens(tokens: Tokens, *, client_id: str = HMS_APP_ID,
                   timeout: int = 30) -> Tokens:
    """Rotate the access token in place; raises HuaweiAuthError when the pair is spent."""
    if not tokens.refresh_token:
        raise HuaweiAuthError("no refresh token stored")
    if tokens.refresh_dead:
        raise HuaweiAuthError("refresh token older than its 180 days")
    url = f"{tokens.session_host or SESSION_HOSTS[0]}{REFRESH_PATH}"
    body = {"refreshToken": tokens.refresh_token, "appId": str(client_id)}
    response = _json_request("POST", url, headers=_session_headers(tokens, tokens.uid),
                             body=body, timeout=timeout, raw=True)
    if _result_code(response) == BAD_HUID_CODE and tokens.uid:
        tokens.uid = None
        response = _json_request("POST", url, headers=_session_headers(tokens, None),
                                 body=body, timeout=timeout, raw=True)
    ensure_ok(response, url)
    return tokens.apply(response)


def query_access_token(access_token: str, *, client_id: str = HMS_APP_ID,
                       session_host: str | None = None, timeout: int = 30) -> dict:
    """Who does this access token belong to - the answer carries no refresh token."""
    url = f"{session_host or SESSION_HOSTS[0]}{QUERY_PATH}"
    response = _json_request(
        "POST", url, headers=_session_headers(Tokens(), None),
        body={"accessToken": access_token, "appId": str(client_id)}, timeout=timeout,
        raw=True)
    ensure_ok(response, url)
    return response


# ── http ─────────────────────────────────────────────────────────────────


def _json_request(method: str, url: str, *, headers: dict | None = None,
                  body: dict | None = None, params: dict | None = None,
                  timeout: int = 30, raw: bool = False) -> dict:
    """POST/GET JSON over urllib, with the two shapes of failure the cloud uses.

    An unauthenticated ``/dataQuery`` call answers HTTP 200 with resultCode 1005, so the
    status line alone never tells whether the credentials worked; ``raw`` lets the caller
    read the code itself instead of getting an exception, which is what the retry-on-1004
    path needs.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    payload = None
    request_headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, identity",
        "User-Agent": USER_AGENT,
        "x-request-id": str(uuid.uuid4()),
    }
    request_headers.update(headers or {})
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json;charset=utf-8")
    request = urllib.request.Request(url, data=payload, headers=request_headers,
                                     method=method)
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response:
            raw_body = _decode(response.read(), response.headers.get("Content-Encoding"))
    except urllib.error.HTTPError as exc:
        body_bytes = _decode(exc.read(), exc.headers.get("Content-Encoding")
                             if exc.headers else None)
        parsed = _loads(body_bytes, url)
        code = _result_code(parsed)
        if code not in (None, 0, "0"):
            # Some controllers carry the real verdict in the body of a 4xx.
            if not raw:
                ensure_ok(parsed, url)
            return parsed
        raise HuaweiApiError(exc.code, url, body_bytes.decode(
            "utf-8", "replace")[:300]) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HuaweiConnectionError(f"{url}: {getattr(exc, 'reason', exc)}") from exc
    parsed = _loads(raw_body, url)
    if not raw:
        ensure_ok(parsed, url)
    return parsed


def _loads(raw: bytes, url: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise HuaweiApiError(0, url, f"answer is not JSON: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise HuaweiApiError(0, url, f"answer is not an object: {str(parsed)[:200]}")
    return parsed


def _decode(raw: bytes, encoding: str | None) -> bytes:
    if raw and (encoding or "").lower().startswith("gzip"):
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


_ERROR_KEYS = ("resultCode", "code", "retCode")
_DESC_KEYS = ("resultDesc", "errorMsg", "message", "errorDesc", "retMsg")
# Codes that only a fresh browser login can clear.
_FATAL_CODES = (RT_INVALID_CODE, CODE_INVALID_CODE, BAD_HUID_CODE, 1002, 1004, 1005)


def _result_code(payload: dict) -> Any:
    for key in _ERROR_KEYS:
        if payload.get(key) is not None:
            return payload[key]
    return None


def _result_desc(payload: dict) -> str:
    for key in _DESC_KEYS:
        if payload.get(key):
            return str(payload[key])
    return str(payload)[:200]


def ensure_ok(payload: dict, url: str) -> dict:
    """Turn an answer's resultCode into the exception type the caller can act on."""
    code = _result_code(payload)
    if code in (None, 0, "0"):
        return payload
    message = _result_desc(payload)
    if code in _FATAL_CODES:
        raise HuaweiAuthError(f"{url}: {code} - {message}")
    raise HuaweiApiError(code, url, message)


# ── the data controller ──────────────────────────────────────────────────


class HuaweiHealthClient:
    """Reads the account's health data with a self-rotating token pair."""

    def __init__(self, tokens: Tokens, *, host: str | None = None,
                 device_code: int | None = None, version: str = APP_VERSION,
                 token_type: int = TOKEN_TYPE, app_id: str = APP_ID,
                 client_id: str = HMS_APP_ID, language: str | None = None,
                 timeout: int = 30) -> None:
        self.tokens = tokens
        self.host = host or APP_HOSTS[0]
        self.device_code = device_code
        self.version = version
        self.token_type = token_type
        self.app_id = app_id
        self.client_id = client_id
        self.language = language
        self.timeout = timeout

    # ── transport ────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        """The same token, presented three ways, plus the identity headers.

        Suggestion_CloudFactory/gkp.java in the APK sends body token + Authorization +
        x-token together and the cloud accepts it; x-huid alone is enough to make a
        device-bound route answer 20010004 for the wrong pair.
        """
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self.tokens.access_token}",
            "x-token": self.tokens.access_token or "",
            "x-token-type": str(self.token_type),
        }
        if self.tokens.uid:
            headers["x-huid"] = str(self.tokens.uid)
        if self.version:
            headers["x-version"] = self.version
        return headers

    def _body(self, extra: dict | None = None) -> dict:
        payload: dict[str, Any] = {
            "token": self.tokens.access_token or "",
            "tokenType": self.token_type,
            "appId": self.app_id,
            "ts": int(time.time() * 1000),
            "source": 1,
        }
        # A made-up device block fails the same check a missing one passes, so only send
        # what the login actually handed back.
        if self.tokens.site_id is not None:
            payload["siteId"] = self.tokens.site_id
        if self.language:
            payload["language"] = self.language
        if self.device_code:
            payload["deviceCode"] = int(self.device_code)
        payload.update(extra or {})
        return payload

    def post(self, path: str, extra: dict | None = None) -> dict:
        url = f"{self.host}{path}"
        if self.tokens.needs_rotation():
            self.rotate()
        response = self._send(url, extra)
        code = _result_code(response)
        # 1002/1004 means the app (or a previous run) rotated the pair out from under us.
        if code in AUTH_FAILED_CODES and self.tokens.refresh_token:
            _LOGGER.debug("Token rejected (%s), rotating once and retrying", code)
            self.rotate()
            response = self._send(url, extra)
            code = _result_code(response)
        if code not in (None, 0, "0"):
            if code == BAD_HUID_CODE and self.tokens.uid:
                # The stored uid can be from an earlier account; forget it and let the
                # next entry value come from the login answer.
                self.tokens.uid = None
                response = self._send(url, extra)
                code = _result_code(response)
                if code in (None, 0, "0"):
                    return response
            raise HuaweiApiError(code, url, _result_desc(response))
        return response

    def _send(self, url: str, extra: dict | None) -> dict:
        return _json_request("POST", url, headers=self._headers(),
                             body=self._body(extra), timeout=self.timeout, raw=True)

    def rotate(self) -> None:
        refresh_tokens(self.tokens, client_id=self.client_id, timeout=self.timeout)

    # ── endpoints ────────────────────────────────────────────────────────
    def sports_stat(self, start: int, end: int, device_code: int | None = None) -> dict:
        """Daily totals per sport type - the numbers the Health app's charts show."""
        body: dict[str, Any] = {"startTime": int(start), "endTime": int(end),
                                "dataSource": 2}
        code = self.device_code if device_code is None else device_code
        if code:
            body["deviceCode"] = int(code)
        return self.post(READ_PATHS["sports_stat"], body)

    def sports_daily(self, start: int, end: int) -> dict:
        return self.post(READ_PATHS["sports_daily"], {
            "startTime": int(start), "endTime": int(end), "dataSource": 2})

    def sports_detail(self, start: int, end: int, sport_types: Iterable[int],
                      query_type: int = 1, data_type: int = 2) -> dict:
        return self.post(READ_PATHS["sports_detail"], {
            "startTime": int(start), "endTime": int(end),
            "queryType": int(query_type), "dataType": int(data_type),
            "sportTypes": [int(t) for t in sport_types]})

    def health_stat(self, start: int, end: int, types: Iterable[int],
                    device_code: int = 0) -> dict:
        return self.post(READ_PATHS["health_stat"], {
            "startTime": int(start), "endTime": int(end),
            "types": [int(t) for t in types],
            "deviceCode": int(device_code), "dataSource": 2})

    def health_data(self, start: int, end: int, type_id: int, query_type: int = 1,
                    data_type: int = 2) -> dict:
        return self.post(READ_PATHS["health_data"], {
            "startTime": int(start), "endTime": int(end), "type": int(type_id),
            "queryType": int(query_type), "dataType": int(data_type)})

    def bind_devices(self) -> dict:
        return self.post(READ_PATHS["bind_devices"], {})

    def _by_time_range(self, fetch: Callable[[int, int], dict], start: int,
                       end: int) -> list[dict]:
        """Run a *ByTime query over a YYYYMMDD range in the <=10-day windows it allows."""
        records: list[dict] = []
        seen: set[Any] = set()
        for window_start, window_end in day_windows(start, end):
            response = fetch(window_start, window_end)
            for rows in (response.get("data") or {}).values():
                for record in rows or []:
                    if not isinstance(record, dict):
                        continue
                    key = record.get("dataId")
                    if key is not None:
                        if key in seen:
                            continue
                        seen.add(key)
                    records.append(record)
        return records

    # ── shaped reads ─────────────────────────────────────────────────────
    def daily_summary(self, days: int = 3) -> list[dict]:
        """One row per (day, sportType) of the account's daily activity."""
        end = day_int(date.today())
        start = day_int(date.today() - timedelta(days=max(days, 1) - 1))
        response = self.sports_stat(start, end)
        return daily_activity(response.get("sportStat") or [])

    def sessions(self, days: int = 7) -> list[dict]:
        """Recorded workouts, rebuilt from the per-minute segments the cloud stores."""
        end = day_int(date.today())
        start = day_int(date.today() - timedelta(days=max(days, 1) - 1))
        records = self._by_time_range(
            lambda a, b: self.sports_detail(a, b, SESSION_SPORT_TYPES), start, end)
        return sport_sessions(records)

    def health_summary(self, days: int = 3,
                       types: Iterable[int] = (7, 9, 11, 12)) -> list[dict]:
        end = day_int(date.today())
        start = day_int(date.today() - timedelta(days=max(days, 1) - 1))
        response = self.health_stat(start, end, types=types)
        return health_series(response)


# ── response shaping ─────────────────────────────────────────────────────


def daily_activity(rows: Iterable[dict]) -> list[dict]:
    """One table row per recordDay of a getSportsStat/getSportsDimenStat answer."""
    out = []
    for record in rows:
        if not isinstance(record, dict):
            continue
        day = normalize_day(record.get("recordDay"))
        if not day:
            continue
        basic = record.get("sportBasicInfo") or {}
        walk = record.get("dimenDailyActivity") or {}
        goal = record.get("goalAchieveBasic") or {}
        exercise = record.get("exerciseTimeBasic") or {}
        active = record.get("activeHourBasic") or {}
        out.append({
            "date": day.isoformat(),
            "sport_type": record.get("sportType"),
            # calorie is 0.001 kcal and distance is metres on the wire.
            "steps": to_int(basic.get("steps")),
            "distance_m": to_int(basic.get("distance")),
            "kcal": _round(to_float(basic.get("calorie"))),
            "duration_min": to_int(basic.get("duration")),
            "walk_min": to_int(walk.get("walkDurations")),
            "active_hours": to_int(active.get("countActiveHour")),
            "exercise_min": to_int(exercise.get("totalMidHighIntensity")),
            "step_goal": to_int(goal.get("stepGoalValueStat")),
        })
    out.sort(key=lambda row: (row["date"], row["sport_type"] or 0))
    return out


def _round(calorie_milli: float | None) -> float | None:
    return None if calorie_milli is None else round(calorie_milli / 1000, 1)


def daily_totals(rows: Iterable[dict]) -> dict[str, dict]:
    """Collapse daily_activity rows to {ISO date: row}.

    getSportsStat answers with one aggregate row per day (sportType 0) plus, depending on
    the account's devices, per-activity rows; the aggregate is the one that matches the
    Health app's own rings, so it wins over any summing.
    """
    out: dict[str, dict] = {}
    for row in rows:
        day = row["date"]
        current = out.get(day)
        if current is None or (not row["sport_type"] and current["sport_type"]):
            out[day] = row
    return out


# getHealthStat answers one list per family, each record carrying a nested *Basic block.
HEALTH_FAMILIES = {
    "heartRateTotal": ("heartRateBasic", {
        "lastRestHeartRate": "resting_heart_rate",
        "lastHeartRate": "heart_rate",
        "avgRestingHeartRate": "average_resting_heart_rate",
        "maxHeartRate": "max_heart_rate",
        "minHeartRate": "min_heart_rate"}),
    "professionalSleepTotal": ("professionalSleep", {
        "allSleepTime": "sleep_duration",
        "deepSleepTime": "sleep_deep",
        "lightSleepTime": "sleep_light",
        "dreamTime": "sleep_rem",
        "awakeTime": "sleep_awake",
        "sleepScore": "sleep_score",
        "sleepEfficiency": "sleep_efficiency",
        "sleepLatency": "sleep_latency",
        "wakeupCnt": "sleep_wake_ups",
        "lastAvgHeartrate": "sleep_average_heart_rate",
        "lastAvgHrv": "sleep_hrv",
        "lastAvgSpO2": "sleep_spo2"}),
    "stressTotal": ("stressBasic", {
        "meanScore": "stress_average",
        "lastScore": "stress_last",
        "maxScore": "stress_max",
        "minScore": "stress_min",
        "measureCount": "stress_measurements"}),
    "exerciseIntensityTotal": ("exerciseIntensityBasic", {
        "totalMidHighIntensity": "moderate_high_intensity_minutes"}),
}


def health_series(payload: dict) -> list[dict]:
    """Per-day health metrics, whatever families the account's devices fill."""
    rows: dict[str, dict] = {}
    for family, (block, fields) in HEALTH_FAMILIES.items():
        for record in payload.get(family) or []:
            if not isinstance(record, dict):
                continue
            day = normalize_day(record.get("recordDay"))
            values = record.get(block) or {}
            if not day or not isinstance(values, dict):
                continue
            row = rows.setdefault(day.isoformat(), {"date": day.isoformat()})
            for key, name in fields.items():
                got = to_float(values.get(key))
                # 0 and -1 are the cloud's "the band never measured this", not a reading.
                if got is not None and got > 0:
                    row[name] = got
            if block == "professionalSleep":
                # A night belongs to the day it woke up in, so carry its real bounds.
                for key, name in (("fallAsleepTime", "fall_asleep"),
                                  ("wakeupTime", "wakeup")):
                    stamp = to_float(values.get(key))
                    if stamp and stamp > 1e12:
                        row[name] = datetime.fromtimestamp(stamp / 1000).strftime(
                            "%Y-%m-%d %H:%M")
    return [rows[key] for key in sorted(rows)]


def sport_sessions(records: Iterable[dict], gap_minutes: int = 15) -> list[dict]:
    """Rebuild workouts from per-minute segments.

    Every device that saw a minute of the workout uploads its own copy of it (band and
    phone both appear for one ride), so summing raw segments double counts. Keep the
    fullest record per clock minute per sport type, then stitch consecutive minutes.
    """
    by_type: dict[Any, dict[int, dict]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        start = to_float(record.get("startTime"))
        end = to_float(record.get("endTime"))
        if not start or not end:
            continue
        basic = (record.get("sportBasicInfos") or [{}])[0]
        weight = (to_float(basic.get("distance")) or 0, to_float(basic.get("steps")) or 0,
                  to_float(basic.get("calorie")) or 0)
        minutes = by_type.setdefault(to_int(record.get("sportType")), {})
        key = int(start // 60000)
        if key not in minutes or weight > minutes[key]["weight"]:
            minutes[key] = {"start_ms": start, "end_ms": end, "basic": basic,
                            "device_code": record.get("deviceCode"), "weight": weight}

    sessions: list[dict] = []
    for sport_type, minutes in sorted(by_type.items(), key=lambda kv: kv[0] or 0):
        for _, entry in sorted(minutes.items()):
            basic = entry["basic"]
            start, end = entry["start_ms"], entry["end_ms"]
            duration = to_int(basic.get("duration")) or 0
            if (sessions and sessions[-1]["sport_type"] == sport_type
                    and start - sessions[-1]["end_ms"] <= gap_minutes * 60000):
                last = sessions[-1]
                last["end_ms"] = max(last["end_ms"], end)
                last["duration_min"] += duration
                last["steps"] += to_int(basic.get("steps")) or 0
                last["distance_m"] += to_int(basic.get("distance")) or 0
                last["kcal"] += (to_float(basic.get("calorie")) or 0) / 1000
                last["segments"] += 1
                continue
            sessions.append({
                "sport_type": sport_type,
                "start_ms": start, "end_ms": end,
                "duration_min": duration,
                "steps": to_int(basic.get("steps")) or 0,
                "distance_m": to_int(basic.get("distance")) or 0,
                "kcal": (to_float(basic.get("calorie")) or 0) / 1000,
                "segments": 1,
                "device_code": entry["device_code"],
            })
    for session in sessions:
        session["start"] = _stamp(session.pop("start_ms"))
        session["end"] = _stamp(session.pop("end_ms"))
        session["kcal"] = round(session["kcal"], 1)
    sessions.sort(key=lambda session: session["start"])
    return sessions


def _stamp(ms: float | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000)
