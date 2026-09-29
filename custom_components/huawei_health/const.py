"""Constants for the Huawei Health integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "huawei_health"
PLATFORMS: Final = ["sensor"]

# ── protocol ─────────────────────────────────────────────────────────────
# The Health app's own cloud. One host per GRS country group; the RU one is what the
# account's app talks to, and every group answers for any client (verified live).
APP_HOSTS: Final = (
    "https://sportdata-drru.things.dbankcloud.ru",
    "https://sportdata-dre.things.dbankcloud.com",
    "https://sportdata-dra.things.dbankcloud.com",
    "https://healthdata.dbankcloud.cn",
)

# Token issuing/rotation lives on a different controller than the data (GRS key
# domainHealthCloudCommon).
SESSION_HOSTS: Final = (
    "https://healthcommon-drru.things.dbankcloud.ru",
    "https://healthcommon-dre.things.dbankcloud.com",
)

OAUTH_BASE: Final = "https://oauth-login.cloud.huawei.com/oauth2"
AUTHORIZE_PATH: Final = "/v3/authorize"
OBTAIN_PATH: Final = "/commonAbility/userAccessToken/obtain"
REFRESH_PATH: Final = "/commonAbility/userAccessToken/refresh"
QUERY_PATH: Final = "/commonAbility/userAccessToken/query"

# The app id the token pair is minted for - com.huawei.health.BuildConfig.HMS_APPLICATION_ID.
# It is a *number*, not the package name, and a code traded with the wrong one is refused.
HMS_APP_ID: Final = "10414141"
APP_ID: Final = "com.huawei.health"
TOKEN_TYPE: Final = 2
# x-version is "and_health_" + versionName of the Health release the protocol came from.
APP_VERSION: Final = "and_health_16.1.6.320"
# ThirdLoginDataStorageUtil.REFRESH_TOKEN_INTERVAL, in seconds.
REFRESH_TOKEN_TTL: Final = 180 * 24 * 3600
# The access token lives tens of minutes; rotate ahead of the wall instead of on a failure.
TOKEN_LIVE_SLACK: Final = 120

REDIRECT_URI: Final = "hms://redirect_url"
SCOPES: Final = (
    "https://www.huawei.com/auth/account/base.profile",
    "https://www.huawei.com/healthkit/step.both",
    "https://www.huawei.com/healthkit/activityrecord.both",
)

READ_PATHS: Final = {
    "sports_stat": "/dataQuery/sport/v2/getSportsStat",
    "sports_daily": "/dataQuery/sport/getSportsDimenStat",
    "sports_detail": "/dataQuery/sport/getSportsDataByTime",
    "health_stat": "/dataQuery/health/getHealthStat",
    "health_data": "/dataQuery/health/getHealthData",
    "sync_versions": "/dataQuery/common/getSyncVersions",
    "bind_devices": "/profile/device/getBindDevice",
}

# getHealthStat/getHealthData type ids, named after the samplePoint keys in the answer.
HEALTH_TYPES: Final = {7: "heart_rate", 9: "professional_sleep", 11: "stress",
                       12: "exercise_intensity"}
# sportTypes for the *ByTime endpoints. 5 is the phone/band's automatic minute-by-minute
# walking track (it covers the whole day, so it is not a workout), 6/7/8 are sleep segments.
# 14 shows up in the data but the app never names it, so it reads as unknown.
SPORT_TYPES: Final = {1: "stairs", 2: "hill", 3: "cycling", 4: "running", 5: "walking",
                      6: "deep_sleep", 7: "light_sleep", 8: "awake", 9: "swimming",
                      10: "other", 14: "unknown"}
SESSION_SPORT_TYPES: Final = tuple(t for t in SPORT_TYPES if t not in (5, 6, 7, 8))

# getSportsDataByTime/getHealthData answer 1001 for a span wider than ten days, whatever
# stamp format is used, so a longer pull has to be chunked.
BY_TIME_WINDOW_DAYS: Final = 10

# resultCode -> what to do about it.
AUTH_FAILED_CODES: Final = (1002, 1004)
RT_INVALID_CODE: Final = 20020003
CODE_INVALID_CODE: Final = 20020001
BAD_HUID_CODE: Final = 20010004
INVALID_DEVICE_CODE: Final = 30005

# ── Home Assistant config keys ───────────────────────────────────────────
CONF_UID: Final = "uid"
CONF_SITE_ID: Final = "site_id"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_REFRESH_EXPIRES_AT: Final = "refresh_expires_at"
CONF_HOST: Final = "api_host"
CONF_SESSION_HOST: Final = "session_host"
CONF_TOKEN_TYPE: Final = "token_type"
CONF_DEVICE_CODE: Final = "device_code"
CONF_HISTORY_DAYS: Final = "history_days"
CONF_WORKOUT_DAYS: Final = "workout_days"
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_ENABLE_WORKOUTS: Final = "enable_workouts"
CONF_ENABLE_HEALTH: Final = "enable_health_metrics"
CONF_CODE: Final = "code"

DEFAULT_UPDATE_INTERVAL: Final = timedelta(minutes=15)
DEFAULT_HISTORY_DAYS: Final = 3
DEFAULT_WORKOUT_DAYS: Final = 7
MIN_HISTORY_DAYS: Final = 1
MAX_HISTORY_DAYS: Final = 30
