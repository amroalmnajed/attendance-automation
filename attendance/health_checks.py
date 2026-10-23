"""attendance/health_checks.py — Startup secret & environment health checks (T9).

Verifies that required environment variables and secrets (sheet IDs, API keys, credentials)
are set and not left as empty, placeholder, or stale values.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger("health_checks")

PLACEHOLDER_SUBSTRINGS = (
    "placeholder",
    "example",
    "change_me",
    "changeme",
    "dummy",
    "your-",
    "your_",
    "<your-",
    "<optional-",
)


def is_placeholder(val: Optional[str]) -> bool:
    """Check if an environment variable value looks like a template placeholder."""
    if val is None:
        return False
    s = val.strip().lower()
    if not s:
        return True
    if s.startswith("<") and s.endswith(">"):
        return True
    for sub in PLACEHOLDER_SUBSTRINGS:
        if sub in s:
            return True
    return False


def find_secret_issues(
    cfg: Optional[dict] = None,
    env: Optional[dict] = None,
) -> list[dict]:
    """Inspect environment variables for sheet IDs and core secrets.

    Returns a list of issue dicts:
      {"var": str, "issue": "missing" | "empty" | "placeholder" | "file_not_found", "message": str}
    """
    env_map = os.environ if env is None else env
    cfg = cfg or {}
    att_cfg = cfg.get("attendance", {}) or {}
    issues: list[dict] = []

    # 1. Course sheet IDs
    courses = att_cfg.get("courses", {}) or {}
    if isinstance(courses, dict):
        for course_key, course_info in courses.items():
            if isinstance(course_info, dict):
                sheet_var = course_info.get("sheet_id_env")
                if sheet_var:
                    if sheet_var not in env_map:
                        issues.append({
                            "var": sheet_var,
                            "issue": "missing",
                            "message": f"Sheet ID env var {sheet_var} for course '{course_key}' is not set.",
                        })
                    else:
                        val = env_map.get(sheet_var)
                        if val is None or not str(val).strip():
                            issues.append({
                                "var": sheet_var,
                                "issue": "empty",
                                "message": f"Sheet ID env var {sheet_var} for course '{course_key}' is empty.",
                            })
                        elif is_placeholder(str(val)):
                            issues.append({
                                "var": sheet_var,
                                "issue": "placeholder",
                                "message": f"Sheet ID env var {sheet_var} for course '{course_key}' has a placeholder value.",
                            })

    # 2. Mirror sheet ID (if configured)
    mirror_var = att_cfg.get("mirror_sheet_id_env")
    if mirror_var:
        if mirror_var not in env_map:
            issues.append({
                "var": mirror_var,
                "issue": "missing",
                "message": f"Mirror sheet ID env var {mirror_var} is not set.",
            })
        else:
            val = env_map.get(mirror_var)
            if val is None or not str(val).strip():
                issues.append({
                    "var": mirror_var,
                    "issue": "empty",
                    "message": f"Mirror sheet ID env var {mirror_var} is empty.",
                })
            elif is_placeholder(str(val)):
                issues.append({
                    "var": mirror_var,
                    "issue": "placeholder",
                    "message": f"Mirror sheet ID env var {mirror_var} has a placeholder value.",
                })

    # 3. OCR API keys (FreeAI primary, Gemini fallback)
    ocr_cfg = att_cfg.get("ocr", {}) or {}
    provider = ocr_cfg.get("provider", "freeai")

    if provider == "freeai":
        freeai_key_var = ocr_cfg.get("freeai_api_key_env", "FREEAI_API_KEY")
        freeai_val = env_map.get(freeai_key_var)
        if freeai_key_var not in env_map:
            issues.append({
                "var": freeai_key_var,
                "issue": "missing",
                "message": f"Required secret {freeai_key_var} is not set.",
            })
        elif freeai_val is None or not str(freeai_val).strip():
            issues.append({
                "var": freeai_key_var,
                "issue": "empty",
                "message": f"Required secret {freeai_key_var} is empty.",
            })
        elif is_placeholder(str(freeai_val)):
            issues.append({
                "var": freeai_key_var,
                "issue": "placeholder",
                "message": f"Required secret {freeai_key_var} contains a placeholder value.",
            })

    if provider == "gemini":
        gemini_val = env_map.get("GEMINI_API_KEY")
        if "GEMINI_API_KEY" not in env_map:
            issues.append({
                "var": "GEMINI_API_KEY",
                "issue": "missing",
                "message": "Required secret GEMINI_API_KEY is not set.",
            })
        elif gemini_val is None or not str(gemini_val).strip():
            issues.append({
                "var": "GEMINI_API_KEY",
                "issue": "empty",
                "message": "Required secret GEMINI_API_KEY is empty.",
            })
        elif is_placeholder(str(gemini_val)):
            issues.append({
                "var": "GEMINI_API_KEY",
                "issue": "placeholder",
                "message": "Required secret GEMINI_API_KEY contains a placeholder value.",
            })
    elif provider == "freeai" and ocr_cfg.get("fallback_to_gemini", True):
        # Validate GEMINI_API_KEY if configured for fallback
        if "GEMINI_API_KEY" in env_map:
            gemini_val = env_map.get("GEMINI_API_KEY")
            if gemini_val is None or not str(gemini_val).strip():
                issues.append({
                    "var": "GEMINI_API_KEY",
                    "issue": "empty",
                    "message": "Fallback secret GEMINI_API_KEY is empty.",
                })
            elif is_placeholder(str(gemini_val)):
                issues.append({
                    "var": "GEMINI_API_KEY",
                    "issue": "placeholder",
                    "message": "Fallback secret GEMINI_API_KEY contains a placeholder value.",
                })

    # 4. Google Application Credentials
    core_auth = (cfg.get("core", {}) or {}).get("auth", {}) or {}
    cred_var = core_auth.get("credentials_env", "GOOGLE_APPLICATION_CREDENTIALS")
    if cred_var in env_map:
        val = env_map.get(cred_var)
        if val is None or not str(val).strip():
            issues.append({
                "var": cred_var,
                "issue": "empty",
                "message": f"Credentials env var {cred_var} is set but empty.",
            })
        elif is_placeholder(str(val)):
            issues.append({
                "var": cred_var,
                "issue": "placeholder",
                "message": f"Credentials env var {cred_var} contains a placeholder value.",
            })
        elif not Path(str(val)).exists():
            issues.append({
                "var": cred_var,
                "issue": "file_not_found",
                "message": f"Credentials file at {val} ({cred_var}) does not exist.",
            })

    return issues


def check_secrets(
    cfg: Optional[dict] = None,
    env: Optional[dict] = None,
    log_alerts: bool = True,
    print_alerts: bool = True,
) -> list[str]:
    """Inspect environment variables, log alerts and print to stdout if any secret is missing or stale.

    Returns the list of warning messages.
    """
    issues = find_secret_issues(cfg, env)
    messages: list[str] = []

    for issue in issues:
        msg = issue["message"]
        messages.append(msg)
        if log_alerts:
            log.warning(
                "secret_health_check_failed",
                extra={"var": issue["var"], "issue": issue["issue"], "detail": msg},
            )
        if print_alerts:
            print(f"[HEALTH CHECK WARNING] {msg}", file=sys.stdout)

    return messages
