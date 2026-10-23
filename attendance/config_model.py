"""attendance/config_model.py — Pydantic models for attendance.yaml (T10).

Provides structured Pydantic v2 validation replacing manual checks in config_check.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(ValueError):
    """The attendance config is unusable; the message says what to fix (no secrets)."""


class BaseConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")


# ─── Core Section Models ──────────────────────────────────────────────────────

class AppConfig(BaseConfigModel):
    env: Optional[str] = "dev"
    timezone: Optional[str] = "Africa/Cairo"
    log_level: Optional[str] = "INFO"
    dry_run_default: Optional[bool] = False


class AuthConfig(BaseConfigModel):
    credentials_env: Optional[str] = "GOOGLE_APPLICATION_CREDENTIALS"
    secrets_backend: Optional[str] = "env"


class NotifyConfig(BaseConfigModel):
    channel: Optional[str] = "log"
    recipients_env: Optional[str] = "NOTIFY_TO"
    include_student_names: Optional[bool] = False


class RetentionConfig(BaseConfigModel):
    image_days: Optional[int] = 30
    flagged_image_days_after_resolve: Optional[int] = 30
    audit_log: Optional[str] = "keep"


class CoreConfig(BaseConfigModel):
    app: Optional[AppConfig] = None
    auth: Optional[AuthConfig] = None
    notify: Optional[NotifyConfig] = None
    retention: Optional[RetentionConfig] = None


# ─── Attendance Sub-models ────────────────────────────────────────────────────

class MultiPhotoConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    max_photos: Optional[int] = 6
    append_window_s: Optional[int] = 120


class IngestConfig(BaseConfigModel):
    channel: Optional[str] = "cli"
    poll_interval_s: Optional[int] = 300
    form_responses_sheet_id_env: Optional[str] = "INGEST_SHEET_ID"
    inbox_folder_id_env: Optional[str] = "INBOX_FOLDER_ID"
    fields: Optional[Dict[str, str]] = None
    names_preview: Optional[str] = "on_issues"
    multi_photo: Optional[MultiPhotoConfig] = None


class AuthorizedConfig(BaseConfigModel):
    telegram_ids: Optional[List[Any]] = Field(default_factory=list)


class RemindersConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    grace_minutes: Optional[int] = 60
    window_hours: Optional[int] = 12
    check_every_minutes: Optional[int] = 10
    recipients: Optional[str] = "both"
    schedule: Optional[Dict[str, Any]] = None


class HealthConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    window_minutes: Optional[int] = 60
    gemini_threshold: Optional[int] = 5
    sheets_threshold: Optional[int] = 3
    review_max_age_days: Optional[int] = 3
    cooldown_minutes: Optional[int] = 120


class SummaryCompareConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    drop_threshold_pct: Optional[int] = 30
    min_previous: Optional[int] = 5


class AbsentButtonConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    courses: Optional[List[str]] = Field(default_factory=list)
    min_matched: Optional[int] = 3
    block_when_pending_review: Optional[bool] = True


class PreprocessConfig(BaseConfigModel):
    orient: Optional[bool] = True
    deskew: Optional[bool] = True
    contrast: Optional[str] = "mild"
    max_side_px: Optional[int] = 2000
    strip_exif: Optional[bool] = True


class SecondPassConfig(BaseConfigModel):
    mode: Optional[str] = "full"
    model: Optional[str] = None
    provider: Optional[str] = None
    include_unmatched: Optional[bool] = True


class OcrConfig(BaseConfigModel):
    provider: Optional[str] = "gemini"
    model: Optional[str] = "gemini-3.5-flash"
    freeai_api_key_env: Optional[str] = "FREEAI_API_KEY"
    fallback_to_gemini: Optional[bool] = True
    passes: Optional[int] = 2
    timeout_s: Optional[int] = 60
    max_attempts: Optional[int] = 3
    second_provider: Optional[str] = None
    second_pass: Optional[SecondPassConfig] = None
    min_confidence: Optional[float] = 0.75
    send_roster_to_provider: Optional[bool] = True
    preprocess: Optional[PreprocessConfig] = None
    retry_preprocessing: Optional[bool] = False


class VariantLearningConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    suggestions_file: Optional[str] = "config/variant_suggestions.json"
    min_similarity: Optional[float] = 0.7


class MatchWeightsConfig(BaseConfigModel):
    token_sort: Optional[float] = 0.5
    token_set: Optional[float] = 0.5


class MatchConfig(BaseConfigModel):
    t_high: Optional[int] = 90
    margin: Optional[int] = 10
    review_floor: Optional[int] = 60
    min_tokens_for_auto: Optional[int] = 1
    t_soft: Optional[int] = None
    margin_strong: Optional[int] = 20
    learn_aliases: Optional[bool] = True
    aliases_file: Optional[str] = "config/learned_aliases.json"
    alias_min_confirmations: Optional[int] = 1
    variant_learning: Optional[VariantLearningConfig] = None
    alias_stale_days: Optional[int] = 90
    weights: Optional[MatchWeightsConfig] = None
    variants_file: Optional[str] = "config/name_variants.yaml"
    hidden_rows: Optional[str] = "exclude"
    skip_row_patterns: Optional[List[str]] = Field(default_factory=list)
    missing_student_policy: Optional[str] = "report"


class WriteRetryConfig(BaseConfigModel):
    max_attempts: Optional[int] = 6
    base_s: Optional[float] = 1.0
    cap_s: Optional[float] = 32.0
    jitter: Optional[bool] = True


class WriteConfig(BaseConfigModel):
    session_check: Optional[str] = "strict"
    overwrite: Optional[str] = "refuse"
    values_source: Optional[str] = "sheet"
    retry: Optional[WriteRetryConfig] = None


class ReviewConfig(BaseConfigModel):
    store: Optional[str] = "local_json"
    local_dir: Optional[str] = "review_queue"
    mirror_sheet_id_env: Optional[str] = "OPS_SHEET_ID"
    tab: Optional[str] = "Review"


class ZoomApiConfig(BaseConfigModel):
    account_id_env: Optional[str] = "ZOOM_ACCOUNT_ID"
    client_id_env: Optional[str] = "ZOOM_CLIENT_ID"
    client_secret_env: Optional[str] = "ZOOM_CLIENT_SECRET"
    token_url: Optional[str] = "https://zoom.us/oauth/token"
    api_base_url: Optional[str] = "https://api.zoom.us/v2"


class ZoomCsvConfig(BaseConfigModel):
    enabled: Optional[bool] = True
    meeting_id_mappings: Optional[Dict[str, str]] = None
    teacher_name: Optional[str] = "Your Teacher Name"
    teacher_fuzzy_match_threshold: Optional[int] = 80
    teacher_join_buffer_minutes: Optional[int] = 5
    late_threshold_minutes: Optional[int] = 15
    min_duration_minutes: Optional[int] = 45
    early_leave_threshold_minutes: Optional[int] = 20
    duplicate_handling: Optional[str] = "sum_duration"
    auto_session_type: Optional[bool] = True
    auto_archive_csv: Optional[bool] = True
    archive_folder: Optional[str] = "csv_archive"
    auto_learn_meeting_ids: Optional[bool] = True
    csv_strict_validation: Optional[bool] = True
    log_folder: Optional[str] = "logs/zoom_csv"
    zoom_api: Optional[ZoomApiConfig] = None
    match_overrides: Optional[Dict[str, Any]] = None


# ─── Course Config ────────────────────────────────────────────────────────────

def _types_ok(types: Any) -> bool:
    """True if types mapping contains at least one non-blank string in some bucket."""
    if not isinstance(types, dict) or not types:
        return False
    return any(
        isinstance(bucket, (list, tuple)) and any(isinstance(t, str) and t.strip() for t in bucket)
        for bucket in types.values()
    )


class CourseConfig(BaseConfigModel):
    sheet_id_env: Optional[str] = None
    tab: Optional[str] = None
    layout: Optional[str] = "single"
    name_col: Optional[str] = "A"
    header_row: Optional[int] = 1
    session_header_pattern: Optional[str] = None
    attendance_header_pattern: Optional[str] = None
    date_row: Optional[int] = None
    first_student_row: Optional[int] = 5
    hidden_rows: Optional[List[int]] = None
    hidden_cols: Optional[List[str]] = None
    allowed_values: Optional[List[str]] = None
    types: Optional[Any] = None

    @model_validator(mode="after")
    def validate_typed_pair_types(self) -> CourseConfig:
        if self.layout == "typed_pair" and not _types_ok(self.types):
            raise ValueError("layout is typed_pair but 'types' is missing or empty (add at least one session type)")
        return self


# ─── Root Attendance Config ───────────────────────────────────────────────────

class AttendanceConfig(BaseConfigModel):
    ingest: Optional[IngestConfig] = None
    authorized: Optional[AuthorizedConfig] = None
    reminders: Optional[RemindersConfig] = None
    health: Optional[HealthConfig] = None
    summary_compare: Optional[SummaryCompareConfig] = None
    absent_button: Optional[AbsentButtonConfig] = None
    ocr: Optional[OcrConfig] = None
    match: Optional[MatchConfig] = None
    write: Optional[WriteConfig] = None
    review: Optional[ReviewConfig] = None
    zoom_csv: Optional[ZoomCsvConfig] = None
    attendance_values: Optional[Dict[str, str]] = None
    courses: Optional[Dict[str, CourseConfig]] = None


class FullConfig(BaseConfigModel):
    core: Optional[CoreConfig] = None
    attendance: Optional[AttendanceConfig] = None


# ─── Validation Helpers (drop-in replacement for config_check.py) ─────────────

def config_problems(cfg: Any) -> list[str]:
    """Human-readable problems using Pydantic model validation (empty list = fine). Never raises."""
    if cfg is None:
        return []
    if not isinstance(cfg, dict):
        return ["attendance.courses is missing or malformed"]

    # Check attendance.courses structure specifically for backward compatibility
    att = cfg.get("attendance")
    if att is not None and not isinstance(att, dict):
        return ["attendance section must be a mapping"]
    if isinstance(att, dict) and "courses" in att and att["courses"] is not None:
        courses = att["courses"]
        if not isinstance(courses, dict):
            return ["attendance.courses must be a mapping of course -> settings"]

    try:
        FullConfig.model_validate(cfg)
        return []
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            loc_parts = [str(x) for x in err["loc"] if x != "__root__"]
            loc_str = ".".join(loc_parts)
            msg = err["msg"]
            # Format friendly message for course errors
            if "courses" in loc_parts:
                try:
                    course_idx = loc_parts.index("courses") + 1
                    course_name = loc_parts[course_idx]
                    problems.append(
                        f"course '{course_name}': {msg} (under attendance.courses.{course_name})"
                    )
                    continue
                except (IndexError, ValueError):
                    pass
            if loc_str:
                problems.append(f"{loc_str}: {msg}")
            else:
                problems.append(msg)
        return problems
    except Exception as exc:
        return [str(exc)]


def validate_cfg(cfg: Any) -> FullConfig:
    """Raise ConfigError listing every problem found; return FullConfig when valid."""
    problems = config_problems(cfg)
    if problems:
        raise ConfigError("Invalid config/attendance.yaml: " + "; ".join(problems))
    return FullConfig.model_validate(cfg or {})


def load_config_model(path: str = "config/attendance.yaml") -> FullConfig:
    """Load and validate config/attendance.yaml via FullConfig."""
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return validate_cfg(data)
