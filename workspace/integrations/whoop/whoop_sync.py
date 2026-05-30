#!/usr/bin/env python3
"""WHOOP OAuth + sync bridge for the OpenClaw health workspace."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer/v2"
DEFAULT_SCOPES = (
    "offline read:profile read:body_measurement "
    "read:cycles read:recovery read:sleep read:workout"
)
DEFAULT_LOOKBACK_DAYS = 21
DEFAULT_MAX_PAGES = 20


class ConfigError(RuntimeError):
    """Raised when the local WHOOP setup is incomplete."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return to_iso(utc_now())


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def require(value: str | None, field_name: str) -> str:
    if value:
        return value
    raise ConfigError(f"Missing required config value: {field_name}")


def normalize_scopes(raw_scopes: str) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for scope in ["offline", *raw_scopes.split()]:
        if scope and scope not in seen:
            ordered.append(scope)
            seen.add(scope)
    return " ".join(ordered)


def human_number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{number:.{digits}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def hours_from_milli(value: Any) -> float | None:
    try:
        return float(value) / 3_600_000
    except (TypeError, ValueError):
        return None


def format_duration_milli(value: Any) -> str:
    try:
        total_seconds = int(round(float(value) / 1000))
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def average(values: list[float | int | None]) -> float | None:
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return None
    return sum(cleaned) / len(cleaned)


def local_dt_from_record(record: dict[str, Any], *keys: str) -> datetime | None:
    dt: datetime | None = None
    for key in keys:
        dt = parse_iso(record.get(key))
        if dt:
            break
    if not dt:
        return None
    offset = record.get("timezone_offset")
    if isinstance(offset, str):
        try:
            sign = 1 if offset.startswith("+") else -1
            hours, minutes = offset[1:].split(":")
            tz = timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
            return dt.astimezone(tz)
        except (ValueError, TypeError):
            pass
    return dt.astimezone()


def month_key(record: dict[str, Any], *keys: str) -> str:
    local_dt = local_dt_from_record(record, *keys)
    if not local_dt:
        local_dt = utc_now()
    return local_dt.strftime("%Y-%m")


def sort_timestamp(record: dict[str, Any]) -> str:
    for key in ("updated_at", "end", "start", "created_at"):
        value = record.get(key)
        if value:
            return str(value)
    return ""


def record_id(record: dict[str, Any], fallback_fields: tuple[str, ...]) -> str:
    for field in fallback_fields:
        value = record.get(field)
        if value is not None:
            return str(value)
    raise ConfigError(f"Record missing an identifier field among: {', '.join(fallback_fields)}")


@dataclass
class Config:
    env_file: Path
    workspace_root: Path
    state_root: Path
    output_root: Path
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    scopes: str
    lookback_days: int
    max_pages: int
    token_path: Path
    oauth_state_path: Path
    sync_state_path: Path
    lock_path: Path
    profile_path: Path
    body_path: Path
    latest_path: Path
    daily_dir: Path
    raw_dir: Path


def build_config(env_file: str | None) -> Config:
    home = Path.home()
    env_path = Path(env_file or os.environ.get("WHOOP_ENV_FILE") or (home / ".config" / "whoop-sync.env")).expanduser()
    file_env = parse_env_file(env_path)
    merged = {**file_env, **os.environ}

    def pick(name: str, default: str | None = None) -> str | None:
        value = merged.get(name, default)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    workspace_root = Path(pick("WHOOP_WORKSPACE_ROOT", str(home / ".openclaw" / "workspace"))).expanduser()
    state_root = Path(pick("WHOOP_STATE_DIR", str(home / ".openclaw" / "whoop"))).expanduser()
    output_root = Path(pick("WHOOP_OUTPUT_DIR", str(workspace_root / "health" / "whoop"))).expanduser()
    scopes = normalize_scopes(pick("WHOOP_SCOPES", DEFAULT_SCOPES) or DEFAULT_SCOPES)
    lookback_days = int(pick("WHOOP_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS)) or DEFAULT_LOOKBACK_DAYS)
    max_pages = int(pick("WHOOP_MAX_PAGES", str(DEFAULT_MAX_PAGES)) or DEFAULT_MAX_PAGES)

    return Config(
        env_file=env_path,
        workspace_root=workspace_root,
        state_root=state_root,
        output_root=output_root,
        client_id=pick("WHOOP_CLIENT_ID"),
        client_secret=pick("WHOOP_CLIENT_SECRET"),
        redirect_uri=pick("WHOOP_REDIRECT_URI"),
        scopes=scopes,
        lookback_days=lookback_days,
        max_pages=max_pages,
        token_path=Path(pick("WHOOP_TOKEN_PATH", str(state_root / "token.json"))).expanduser(),
        oauth_state_path=Path(pick("WHOOP_OAUTH_STATE_PATH", str(state_root / "oauth-state.json"))).expanduser(),
        sync_state_path=Path(pick("WHOOP_SYNC_STATE_PATH", str(state_root / "sync-state.json"))).expanduser(),
        lock_path=Path(pick("WHOOP_LOCK_PATH", str(state_root / "sync.lock"))).expanduser(),
        profile_path=output_root / "profile.json",
        body_path=output_root / "body_measurement.json",
        latest_path=output_root / "latest.md",
        daily_dir=output_root / "daily",
        raw_dir=output_root / "raw",
    )


@contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def http_json(
    url: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
) -> Any:
    if params:
        encoded_params = urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{encoded_params}"
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "openclaw-whoop-sync/1.0",
    }
    if headers:
        request_headers.update(headers)
    payload = None
    if form is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        payload = urlencode({k: v for k, v in form.items() if v is not None}).encode("utf-8")
    request = Request(url=url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc}") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON from {url}, got: {body[:240]}") from exc


class WhoopClient:
    def __init__(self, config: Config):
        self.config = config

    def load_token(self) -> dict[str, Any] | None:
        token = load_json(self.config.token_path, default=None)
        if isinstance(token, dict):
            return token
        return None

    def save_token(self, token: dict[str, Any]) -> dict[str, Any]:
        token = dict(token)
        token.setdefault("obtained_at", iso_now())
        expires_in = token.get("expires_in")
        if expires_in is not None:
            try:
                expires_at = parse_iso(token["obtained_at"]) + timedelta(seconds=int(expires_in))
                token["expires_at"] = to_iso(expires_at)
            except (ValueError, TypeError, KeyError):
                pass
        atomic_write_json(self.config.token_path, token)
        return token

    def authorization_url(self) -> tuple[str, dict[str, Any]]:
        client_id = require(self.config.client_id, "WHOOP_CLIENT_ID")
        redirect_uri = require(self.config.redirect_uri, "WHOOP_REDIRECT_URI")
        state = secrets.token_urlsafe(24)
        state_payload = {
            "state": state,
            "created_at": iso_now(),
            "redirect_uri": redirect_uri,
            "scopes": self.config.scopes,
        }
        atomic_write_json(self.config.oauth_state_path, state_payload)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}", state_payload

    def exchange_code(self, code: str, callback_state: str | None = None) -> dict[str, Any]:
        client_id = require(self.config.client_id, "WHOOP_CLIENT_ID")
        client_secret = require(self.config.client_secret, "WHOOP_CLIENT_SECRET")
        redirect_uri = require(self.config.redirect_uri, "WHOOP_REDIRECT_URI")
        saved_state = load_json(self.config.oauth_state_path, default={}) or {}
        if callback_state and saved_state.get("state") and callback_state != saved_state.get("state"):
            raise RuntimeError("OAuth state mismatch. Generate a fresh auth URL and try again.")
        token = http_json(
            TOKEN_URL,
            method="POST",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if not isinstance(token, dict) or "access_token" not in token:
            raise RuntimeError(f"Unexpected token response: {token}")
        if "refresh_token" not in token:
            raise RuntimeError("WHOOP did not return a refresh token. Confirm the offline scope is enabled.")
        return self.save_token(token)

    def refresh_token(self, existing_token: dict[str, Any]) -> dict[str, Any]:
        client_id = require(self.config.client_id, "WHOOP_CLIENT_ID")
        client_secret = require(self.config.client_secret, "WHOOP_CLIENT_SECRET")
        refresh_token = existing_token.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No refresh token available. Re-run the WHOOP authorization flow.")
        refreshed = http_json(
            TOKEN_URL,
            method="POST",
            form={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": self.config.scopes,
            },
        )
        if not isinstance(refreshed, dict) or "access_token" not in refreshed:
            raise RuntimeError(f"Unexpected refresh response: {refreshed}")
        refreshed.setdefault("refresh_token", refresh_token)
        return self.save_token(refreshed)

    def import_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        seed = {
            "refresh_token": refresh_token,
            "scope": self.config.scopes,
            "obtained_at": iso_now(),
        }
        saved = self.save_token(seed)
        return self.refresh_token(saved)

    def ensure_access_token(self) -> str:
        token = self.load_token()
        if not token:
            raise RuntimeError("No WHOOP token found. Run auth-url + exchange first.")
        expires_at = parse_iso(token.get("expires_at"))
        if not token.get("access_token") or not expires_at or utc_now() >= expires_at - timedelta(minutes=5):
            token = self.refresh_token(token)
        access_token = token.get("access_token")
        if not access_token:
            raise RuntimeError("WHOOP token is missing an access token after refresh.")
        return str(access_token)

    def api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        access_token = self.ensure_access_token()
        headers = {"Authorization": f"Bearer {access_token}"}
        return http_json(f"{API_BASE}/{path.lstrip('/')}", headers=headers, params=params)

    def fetch_collection(self, path: str, start: str, end: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        next_token: str | None = None
        pages = 0
        while True:
            pages += 1
            payload = self.api_get(
                path,
                params={
                    "limit": 25,
                    "start": start,
                    "end": end,
                    "nextToken": next_token,
                },
            )
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected collection response for {path}: {payload}")
            page_records = payload.get("records", [])
            if not isinstance(page_records, list):
                raise RuntimeError(f"Expected records list for {path}, got: {payload}")
            records.extend(record for record in page_records if isinstance(record, dict))
            next_token = payload.get("next_token") or payload.get("nextToken")
            if not next_token or pages >= self.config.max_pages:
                break
        return records


def upsert_jsonl(path: Path, records: list[dict[str, Any]], key_fields: tuple[str, ...]) -> None:
    existing: dict[str, dict[str, Any]] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                existing[record_id(record, key_fields)] = record
    for record in records:
        existing[record_id(record, key_fields)] = record
    ordered = sorted(existing.values(), key=sort_timestamp)
    atomic_write_text(path, "".join(json.dumps(record, sort_keys=True) + "\n" for record in ordered))


def write_partitioned_jsonl(
    target_dir: Path,
    records: list[dict[str, Any]],
    key_fields: tuple[str, ...],
    date_keys: tuple[str, ...],
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[month_key(record, *date_keys)].append(record)
    for bucket, bucket_records in grouped.items():
        upsert_jsonl(target_dir / f"{bucket}.jsonl", bucket_records, key_fields)


def date_key(value: datetime | None) -> str | None:
    if not value:
        return None
    return value.strftime("%Y-%m-%d")


def workout_duration(record: dict[str, Any]) -> str:
    start = parse_iso(record.get("start"))
    end = parse_iso(record.get("end"))
    if start and end:
        return format_duration_milli((end - start).total_seconds() * 1000)
    return "unknown"


def build_daily_bundle(
    cycles: list[dict[str, Any]],
    recoveries: list[dict[str, Any]],
    sleeps: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"workouts": []})
    cycle_dates: dict[str, str] = {}
    sleep_dates: dict[str, str] = {}

    for cycle in cycles:
        day = date_key(local_dt_from_record(cycle, "end", "start"))
        if not day:
            continue
        by_day[day]["cycle"] = cycle
        cycle_dates[str(cycle.get("id"))] = day

    for sleep in sleeps:
        day = date_key(local_dt_from_record(sleep, "end", "start"))
        if not day:
            continue
        by_day[day]["sleep"] = sleep
        sleep_dates[str(sleep.get("id"))] = day
        cycle_id = sleep.get("cycle_id")
        if cycle_id is not None and str(cycle_id) not in cycle_dates:
            cycle_dates[str(cycle_id)] = day

    for recovery in recoveries:
        day = None
        cycle_id = recovery.get("cycle_id")
        sleep_id = recovery.get("sleep_id")
        if cycle_id is not None:
            day = cycle_dates.get(str(cycle_id))
        if not day and sleep_id is not None:
            day = sleep_dates.get(str(sleep_id))
        if not day:
            day = date_key(local_dt_from_record(recovery, "updated_at", "created_at"))
        if not day:
            continue
        by_day[day]["recovery"] = recovery

    for workout in workouts:
        day = date_key(local_dt_from_record(workout, "start", "end"))
        if not day:
            continue
        by_day[day]["workouts"].append(workout)

    return by_day


def render_latest_markdown(
    profile: dict[str, Any] | None,
    body: dict[str, Any] | None,
    daily_bundle: dict[str, dict[str, Any]],
    synced_at: str,
) -> str:
    days = sorted(daily_bundle.keys(), reverse=True)
    latest_day = days[0] if days else None
    latest = daily_bundle.get(latest_day or "", {})
    recent_days = [daily_bundle[day] for day in days[:7]]
    recent_recoveries = [day.get("recovery", {}).get("score", {}) for day in recent_days if day.get("recovery")]
    recent_sleeps = [day.get("sleep", {}).get("score", {}) for day in recent_days if day.get("sleep")]
    recent_cycles = [day.get("cycle", {}).get("score", {}) for day in recent_days if day.get("cycle")]
    recent_workouts = []
    for day in recent_days:
        recent_workouts.extend(day.get("workouts", []))
    recent_workouts = sorted(recent_workouts, key=sort_timestamp, reverse=True)[:10]

    name = "unknown"
    email = "unknown"
    if profile:
        first = profile.get("first_name") or ""
        last = profile.get("last_name") or ""
        name = " ".join(part for part in [str(first).strip(), str(last).strip()] if part) or "unknown"
        email = str(profile.get("email") or "unknown")

    body_lines = [
        f"- Height: {human_number(body.get('height_meter'), 3)} m" if body else "- Height: unknown",
        f"- Weight: {human_number(body.get('weight_kilogram'), 1)} kg" if body else "- Weight: unknown",
        f"- Max heart rate: {human_number(body.get('max_heart_rate'), 0)} bpm" if body else "- Max heart rate: unknown",
    ]

    latest_recovery = latest.get("recovery", {}).get("score", {})
    latest_sleep = latest.get("sleep", {}).get("score", {})
    latest_cycle = latest.get("cycle", {}).get("score", {})
    lines = [
        "# WHOOP Latest",
        "",
        f"- Last sync: {synced_at}",
        f"- Latest recovery day: {latest_day or 'unknown'}",
        f"- WHOOP user: {name}",
        f"- WHOOP email: {email}",
        "",
        "## Latest Snapshot",
        f"- Recovery score: {human_number(latest_recovery.get('recovery_score'), 0)}",
        f"- Resting heart rate: {human_number(latest_recovery.get('resting_heart_rate'), 0)} bpm",
        f"- HRV: {human_number(latest_recovery.get('hrv_rmssd_milli'), 1)} ms",
        f"- SpO2: {human_number(latest_recovery.get('spo2_percentage'), 1)}%",
        f"- Skin temp: {human_number(latest_recovery.get('skin_temp_celsius'), 1)} C",
        f"- Sleep duration: {format_duration_milli(latest.get('sleep', {}).get('score', {}).get('stage_summary', {}).get('total_in_bed_time_milli'))}",
        f"- Sleep performance: {human_number(latest_sleep.get('sleep_performance_percentage'), 0)}%",
        f"- Sleep consistency: {human_number(latest_sleep.get('sleep_consistency_percentage'), 0)}%",
        f"- Respiratory rate: {human_number(latest_sleep.get('respiratory_rate'), 1)}",
        f"- Day strain: {human_number(latest_cycle.get('strain'), 1)}",
        f"- Average heart rate: {human_number(latest_cycle.get('average_heart_rate'), 0)} bpm",
        f"- Max heart rate: {human_number(latest_cycle.get('max_heart_rate'), 0)} bpm",
        "",
        "## 7-Day Trend",
        f"- Avg recovery score: {human_number(average([item.get('recovery_score') for item in recent_recoveries]), 1)}",
        f"- Avg sleep duration: {human_number(average([hours_from_milli(item.get('stage_summary', {}).get('total_in_bed_time_milli')) for item in recent_sleeps]), 2)} h",
        f"- Avg sleep performance: {human_number(average([item.get('sleep_performance_percentage') for item in recent_sleeps]), 1)}%",
        f"- Avg HRV: {human_number(average([item.get('hrv_rmssd_milli') for item in recent_recoveries]), 1)} ms",
        f"- Avg resting heart rate: {human_number(average([item.get('resting_heart_rate') for item in recent_recoveries]), 1)} bpm",
        f"- Avg strain: {human_number(average([item.get('strain') for item in recent_cycles]), 1)}",
        f"- Workout count: {len(recent_workouts)}",
        "",
        "## Body Measurement",
        *body_lines,
        "",
        "## Recent Workouts",
    ]

    if recent_workouts:
        for workout in recent_workouts:
            score = workout.get("score", {})
            start_dt = local_dt_from_record(workout, "start")
            lines.append(
                "- "
                + " | ".join(
                    [
                        start_dt.strftime("%Y-%m-%d %H:%M") if start_dt else "unknown time",
                        str(workout.get("sport_name") or f"sport_id={workout.get('sport_id')}"),
                        f"strain {human_number(score.get('strain'), 1)}",
                        f"duration {workout_duration(workout)}",
                        f"avg HR {human_number(score.get('average_heart_rate'), 0)}",
                        f"max HR {human_number(score.get('max_heart_rate'), 0)}",
                    ]
                )
            )
    else:
        lines.append("- No workouts found in the current lookback window.")

    lines.extend(
        [
            "",
            "## Agent Notes",
            "- This file is device-synced from WHOOP. Treat values as device-reported unless otherwise noted.",
            "- Use `daily/` files for date-specific analysis and `raw/` JSONL for exact historical records.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_daily_markdown(day: str, bundle: dict[str, Any], synced_at: str) -> str:
    recovery = bundle.get("recovery", {})
    recovery_score = recovery.get("score", {})
    sleep = bundle.get("sleep", {})
    sleep_score = sleep.get("score", {})
    sleep_stage = sleep_score.get("stage_summary", {})
    cycle = bundle.get("cycle", {})
    cycle_score = cycle.get("score", {})
    workouts = sorted(bundle.get("workouts", []), key=sort_timestamp)

    lines = [
        f"# WHOOP Daily Summary - {day}",
        "",
        f"- Last generated: {synced_at}",
        "",
        "## Recovery",
        f"- Recovery score: {human_number(recovery_score.get('recovery_score'), 0)}",
        f"- Resting heart rate: {human_number(recovery_score.get('resting_heart_rate'), 0)} bpm",
        f"- HRV: {human_number(recovery_score.get('hrv_rmssd_milli'), 1)} ms",
        f"- SpO2: {human_number(recovery_score.get('spo2_percentage'), 1)}%",
        f"- Skin temp: {human_number(recovery_score.get('skin_temp_celsius'), 1)} C",
        "",
        "## Sleep",
        f"- In bed: {format_duration_milli(sleep_stage.get('total_in_bed_time_milli'))}",
        f"- Slow-wave sleep: {format_duration_milli(sleep_stage.get('slow_wave_sleep_milli'))}",
        f"- REM sleep: {format_duration_milli(sleep_stage.get('rem_sleep_milli'))}",
        f"- Sleep performance: {human_number(sleep_score.get('sleep_performance_percentage'), 0)}%",
        f"- Sleep consistency: {human_number(sleep_score.get('sleep_consistency_percentage'), 0)}%",
        f"- Sleep efficiency: {human_number(sleep_score.get('sleep_efficiency_percentage'), 0)}%",
        f"- Respiratory rate: {human_number(sleep_score.get('respiratory_rate'), 1)}",
        "",
        "## Cycle",
        f"- Day strain: {human_number(cycle_score.get('strain'), 1)}",
        f"- Kilojoule: {human_number(cycle_score.get('kilojoule'), 1)}",
        f"- Avg heart rate: {human_number(cycle_score.get('average_heart_rate'), 0)} bpm",
        f"- Max heart rate: {human_number(cycle_score.get('max_heart_rate'), 0)} bpm",
        "",
        "## Workouts",
    ]

    if workouts:
        for workout in workouts:
            score = workout.get("score", {})
            start_dt = local_dt_from_record(workout, "start")
            lines.append(
                "- "
                + " | ".join(
                    [
                        start_dt.strftime("%H:%M") if start_dt else "unknown time",
                        str(workout.get("sport_name") or f"sport_id={workout.get('sport_id')}"),
                        f"strain {human_number(score.get('strain'), 1)}",
                        f"duration {workout_duration(workout)}",
                        f"avg HR {human_number(score.get('average_heart_rate'), 0)}",
                        f"max HR {human_number(score.get('max_heart_rate'), 0)}",
                        f"recorded {human_number(score.get('percent_recorded'), 0)}%",
                    ]
                )
            )
    else:
        lines.append("- No workouts recorded for this day in the current WHOOP window.")

    lines.extend(
        [
            "",
            "## Agent Notes",
            "- Use this file for date-specific reasoning when Dill asks about recovery, sleep, soreness, readiness, or training changes.",
            "- If the user shares a conflicting manual report, keep both and label the discrepancy explicitly.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_generated_files(
    config: Config,
    profile: dict[str, Any] | None,
    body: dict[str, Any] | None,
    cycles: list[dict[str, Any]],
    recoveries: list[dict[str, Any]],
    sleeps: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    synced_at: str,
) -> dict[str, Any]:
    config.output_root.mkdir(parents=True, exist_ok=True)
    config.daily_dir.mkdir(parents=True, exist_ok=True)
    (config.raw_dir / "cycles").mkdir(parents=True, exist_ok=True)
    (config.raw_dir / "recovery").mkdir(parents=True, exist_ok=True)
    (config.raw_dir / "sleep").mkdir(parents=True, exist_ok=True)
    (config.raw_dir / "workouts").mkdir(parents=True, exist_ok=True)

    if profile is not None:
        atomic_write_json(config.profile_path, profile)
    if body is not None:
        atomic_write_json(config.body_path, body)

    write_partitioned_jsonl(config.raw_dir / "cycles", cycles, ("id", "v1_id"), ("end", "start", "updated_at"))
    write_partitioned_jsonl(config.raw_dir / "recovery", recoveries, ("cycle_id", "sleep_id"), ("updated_at", "created_at"))
    write_partitioned_jsonl(config.raw_dir / "sleep", sleeps, ("id", "v1_id"), ("end", "start", "updated_at"))
    write_partitioned_jsonl(config.raw_dir / "workouts", workouts, ("id", "v1_id"), ("start", "end", "updated_at"))

    daily_bundle = build_daily_bundle(cycles, recoveries, sleeps, workouts)
    for day, bundle in daily_bundle.items():
        atomic_write_text(config.daily_dir / f"{day}.md", render_daily_markdown(day, bundle, synced_at))
    atomic_write_text(config.latest_path, render_latest_markdown(profile, body, daily_bundle, synced_at))

    return {
        "days": len(daily_bundle),
        "latest_day": max(daily_bundle.keys()) if daily_bundle else None,
    }


def parse_callback_url(callback_url: str) -> tuple[str, str | None]:
    parsed = urlparse(callback_url)
    query = parse_qs(parsed.query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    if not code:
        raise RuntimeError("Could not find a ?code=... parameter in the callback URL.")
    return str(code), str(state) if state else None


def command_auth_url(client: WhoopClient) -> int:
    auth_url, state_payload = client.authorization_url()
    print(f"Auth URL:\n{auth_url}\n")
    print("Saved OAuth state:")
    print(json.dumps(state_payload, indent=2))
    print("\nNext: open the URL, approve the app, then paste the full callback URL into the exchange command.")
    return 0


def command_exchange(client: WhoopClient, args: argparse.Namespace) -> int:
    code = args.code
    callback_state = None
    if args.callback_url:
        code, callback_state = parse_callback_url(args.callback_url)
    if not code:
        raise ConfigError("Provide either --code or --callback-url.")
    token = client.exchange_code(code, callback_state=callback_state)
    print("WHOOP token saved.")
    print(json.dumps({k: token.get(k) for k in ("scope", "obtained_at", "expires_at")}, indent=2))
    return 0


def command_import_refresh_token(client: WhoopClient, args: argparse.Namespace) -> int:
    token = client.import_refresh_token(args.refresh_token)
    print("WHOOP refresh token imported and validated.")
    print(json.dumps({k: token.get(k) for k in ("scope", "obtained_at", "expires_at")}, indent=2))
    return 0


def command_status(config: Config, client: WhoopClient) -> int:
    token = client.load_token()
    sync_state = load_json(config.sync_state_path, default={}) or {}
    print("WHOOP sync status")
    print(f"- env file: {config.env_file} ({'present' if config.env_file.exists() else 'missing'})")
    print(f"- token file: {config.token_path} ({'present' if config.token_path.exists() else 'missing'})")
    print(f"- output dir: {config.output_root}")
    print(f"- latest summary: {config.latest_path} ({'present' if config.latest_path.exists() else 'missing'})")
    print(f"- lookback days: {config.lookback_days}")
    print(f"- scopes: {config.scopes}")
    if token:
        print(f"- token expires_at: {token.get('expires_at', 'unknown')}")
        print(f"- refresh token present: {'yes' if token.get('refresh_token') else 'no'}")
    else:
        print("- token expires_at: unknown")
        print("- refresh token present: no")
    if sync_state:
        print(f"- last successful sync: {sync_state.get('last_successful_sync_at', 'unknown')}")
        print(f"- last latest day: {sync_state.get('latest_day', 'unknown')}")
        print(f"- last counts: {json.dumps(sync_state.get('counts', {}), sort_keys=True)}")
    else:
        print("- last successful sync: never")
    return 0


def command_sync(config: Config, client: WhoopClient, args: argparse.Namespace) -> int:
    lookback_days = args.lookback_days or config.lookback_days
    start = to_iso(utc_now() - timedelta(days=lookback_days))
    end = to_iso(utc_now() + timedelta(minutes=5))

    with file_lock(config.lock_path):
        sync_state = load_json(config.sync_state_path, default={}) or {}
        sync_state["last_sync_started_at"] = iso_now()
        atomic_write_json(config.sync_state_path, sync_state)

        profile = client.api_get("user/profile/basic")
        body = client.api_get("user/measurement/body")
        cycles = client.fetch_collection("cycle", start, end)
        recoveries = client.fetch_collection("recovery", start, end)
        sleeps = client.fetch_collection("activity/sleep", start, end)
        workouts = client.fetch_collection("activity/workout", start, end)

        synced_at = iso_now()
        generated = write_generated_files(
            config,
            profile if isinstance(profile, dict) else None,
            body if isinstance(body, dict) else None,
            cycles,
            recoveries,
            sleeps,
            workouts,
            synced_at,
        )

        sync_state = {
            "last_sync_started_at": sync_state.get("last_sync_started_at"),
            "last_successful_sync_at": synced_at,
            "lookback_days": lookback_days,
            "latest_day": generated.get("latest_day"),
            "counts": {
                "cycles": len(cycles),
                "recoveries": len(recoveries),
                "sleeps": len(sleeps),
                "workouts": len(workouts),
                "daily_files": generated.get("days"),
            },
        }
        atomic_write_json(config.sync_state_path, sync_state)

    print("WHOOP sync completed.")
    print(json.dumps(sync_state, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WHOOP sync bridge for OpenClaw.")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to the WHOOP env file. Defaults to ~/.config/whoop-sync.env",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-url", help="Generate the WHOOP authorization URL and save OAuth state.")

    exchange = subparsers.add_parser("exchange", help="Exchange a WHOOP auth code for local tokens.")
    exchange.add_argument("--code", help="Raw OAuth code.")
    exchange.add_argument("--callback-url", help="Full redirected callback URL containing ?code=...")

    import_refresh = subparsers.add_parser(
        "import-refresh-token",
        help="Seed the local token store with a WHOOP refresh token and validate it.",
    )
    import_refresh.add_argument("--refresh-token", required=True, help="Refresh token copied from WHOOP/Postman.")

    sync = subparsers.add_parser("sync", help="Fetch WHOOP data and write it into the health workspace.")
    sync.add_argument("--lookback-days", type=int, default=None, help="Override the default sync window.")

    subparsers.add_parser("status", help="Show the local WHOOP sync status.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = build_config(args.env_file)
    client = WhoopClient(config)

    try:
        if args.command == "auth-url":
            return command_auth_url(client)
        if args.command == "exchange":
            return command_exchange(client, args)
        if args.command == "import-refresh-token":
            return command_import_refresh_token(client, args)
        if args.command == "sync":
            return command_sync(config, client, args)
        if args.command == "status":
            return command_status(config, client)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
