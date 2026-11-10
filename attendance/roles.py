"""Role-based bot access (Feature 11). Pure decision logic plus one `guard` decorator.

Granular RBAC with roles: viewer, editor, admin
Permissions: view_attendance, edit_attendance, manage_roster, manage_users, view_audit_log

Legacy support: single "authorized" role via env var and/or config for backward compatibility.
If NO ID is configured anywhere, "authorized" stays open to everyone (the old behaviour).

Fail-safe: if the role check itself fails, the user gets a friendly refusal, never access.
No student/course data ever appears in a refusal; only the user's own Telegram ID.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from attendance.cache import get_cache
from core.logger import get_logger

log = get_logger("roles")

AUTHORIZED_ENV = "AUTHORIZED_TELEGRAM_IDS"

# Legacy role for backward compatibility
AUTHORIZED = "authorized"

# New granular roles
VIEWER = "viewer"
EDITOR = "editor"
ADMIN = "admin"

# Permission definitions
PERMISSIONS = {
    "view_attendance": "View attendance records and review queue",
    "edit_attendance": "Edit attendance records and resolve review items",
    "manage_roster": "Manage student roster and course settings",
    "manage_users": "Manage user roles and permissions",
    "view_audit_log": "View audit logs and system events",
}

# Role-to-permission mapping (admin includes all, editor includes viewer)
ROLE_PERMISSIONS = {
    VIEWER: {"view_attendance", "view_audit_log"},
    EDITOR: {"view_attendance", "edit_attendance", "view_audit_log"},
    ADMIN: {"view_attendance", "edit_attendance", "manage_roster", "manage_users", "view_audit_log"},
}


def _parse(raw: Iterable) -> set[int]:
    out: set[int] = set()
    for item in raw:
        try:
            out.add(int(str(item).strip()))
        except ValueError:
            continue
    return out


def _collect(cfg: Optional[dict], env: Optional[dict], env_name: str, section: str) -> set[int]:
    env = os.environ if env is None else env
    raw: list = str(env.get(env_name, "")).replace(";", ",").split(",")
    sect = (((cfg or {}).get("attendance") or {}).get(section) or {})
    ids = sect.get("telegram_ids") if isinstance(sect, dict) else None
    raw += list(ids) if isinstance(ids, (list, tuple)) else []
    return _parse(raw)


def authorized_ids(cfg: Optional[dict], env: Optional[dict] = None) -> set[int]:
    """Legacy: get authorized IDs from env/config."""
    return _collect(cfg, env, AUTHORIZED_ENV, "authorized")


def role_ids(cfg: Optional[dict], role: str, env: Optional[dict] = None) -> set[int]:
    """Get Telegram IDs for a specific role from config."""
    if role == AUTHORIZED:
        return authorized_ids(cfg, env)
    env = os.environ if env is None else env
    sect = (((cfg or {}).get("attendance") or {}).get("rbac") or {}).get("roles", {})
    role_config = sect.get(role, {}) if isinstance(sect, dict) else {}
    ids = role_config.get("telegram_ids") if isinstance(role_config, dict) else None
    if isinstance(ids, (list, tuple)):
        return _parse(ids)
    return set()


def allowlist_configured(cfg: Optional[dict], env: Optional[dict] = None) -> bool:
    """Check if any RBAC configuration is present."""
    if authorized_ids(cfg, env):
        return True
    rbac_cfg = ((cfg or {}).get("attendance") or {}).get("rbac", {})
    if isinstance(rbac_cfg, dict):
        roles = rbac_cfg.get("roles", {})
        if isinstance(roles, dict):
            for role_config in roles.values():
                if isinstance(role_config, dict) and role_config.get("telegram_ids"):
                    return True
        webapp_users = rbac_cfg.get("webapp_users", {})
        if isinstance(webapp_users, dict) and webapp_users:
            return True
    return False


def _get_db_role(uid: Optional[Any]) -> Optional[str]:
    """Get user role from database. Returns None if user not found or on error."""
    if uid is None:
        return None

    try:
        from attendance.db import get_session
        from attendance.db import User

        cache = get_cache()
        cache_key = f"role:{uid}"
        cached_role = cache.get(cache_key, "permissions")
        if cached_role is not None:
            return cached_role

        session = get_session()
        try:
            user = None
            try:
                uid_int = int(str(uid).strip())
                user = session.query(User).filter(User.telegram_id == uid_int, User.is_active == True).first()
            except ValueError:
                pass
            if not user:
                user = session.query(User).filter(User.username == str(uid).strip(), User.is_active == True).first()
            if user:
                role = user.role
                cache.set(cache_key, role, "permissions", ttl=300)
                return role
        finally:
            session.close()
    except Exception:
        pass  # On any error, fall back to config-based lookups

    return None


def _get_db_course_permissions(uid: Optional[int], course_id: Optional[int]) -> Optional[str]:
    """Get user's permission level for a specific course from database."""
    if uid is None or course_id is None:
        return None

    try:
        from attendance.db import get_session
        from attendance.db import CoursePermission, User

        cache = get_cache()
        cache_key = f"course_perm:{uid}:{course_id}"
        cached_perm = cache.get(cache_key, "permissions")
        if cached_perm is not None:
            return cached_perm

        session = get_session()
        try:
            user = session.query(User).filter(User.telegram_id == uid, User.is_active == True).first()
            if not user:
                return None

            perm = session.query(CoursePermission).filter(
                CoursePermission.user_id == user.user_id,
                CoursePermission.course_id == course_id
            ).first()
            if perm:
                cache.set(cache_key, perm.permission_level, "permissions", ttl=300)
                return perm.permission_level
        finally:
            session.close()
    except Exception as exc:
        log.debug("roles_swallowed", extra={"error": str(exc)})

    return None


def _get_course_id_from_key(course_key: str) -> Optional[int]:
    """Look up course_id from course_key."""
    if not course_key:
        return None

    try:
        from attendance.db import get_session
        from attendance.db import Course

        cache = get_cache()
        cache_key = f"course_id:{course_key}"
        cached_id = cache.get(cache_key, "permissions")
        if cached_id is not None:
            return int(cached_id)

        session = get_session()
        try:
            course = session.query(Course).filter(Course.key == course_key).first()
            if course:
                cache.set(cache_key, str(course.course_id), "permissions", ttl=300)
                return course.course_id
        finally:
            session.close()
    except Exception as exc:
        log.debug("roles_swallowed", extra={"error": str(exc)})

    return None


def _get_default_role(cfg: Optional[dict]) -> Optional[str]:
    """Get the default role from configuration."""
    rbac_cfg = ((cfg or {}).get("attendance") or {}).get("rbac", {})
    if isinstance(rbac_cfg, dict):
        return rbac_cfg.get("default_role")
    return None


def role_of(uid: Optional[Any], cfg: Optional[dict], env: Optional[dict] = None) -> Optional[str]:
    """Determine the role of a user. Returns the highest role (admin > editor > viewer > authorized).

    Checks database first, falls back to config if database has no user records.
    If no specific role is found, uses default_role from config.
    """
    if uid is None:
        return None

    # Check database first
    db_role = _get_db_role(uid)
    if db_role:
        return db_role

    # Check webapp_users from config (supports string usernames, case-insensitive)
    rbac_cfg = ((cfg or {}).get("attendance") or {}).get("rbac", {})
    if isinstance(rbac_cfg, dict):
        webapp_users = rbac_cfg.get("webapp_users", {})
        if isinstance(webapp_users, dict):
            uid_str = str(uid).strip().lower()
            for w_user, w_role in webapp_users.items():
                if str(w_user).strip().lower() == uid_str:
                    return w_role

    # Fallback to config-based lookups for integer Telegram IDs
    try:
        uid_int = int(str(uid).strip())
        if uid_int in role_ids(cfg, ADMIN, env):
            return ADMIN
        if uid_int in role_ids(cfg, EDITOR, env):
            return EDITOR
        if uid_int in role_ids(cfg, VIEWER, env):
            return VIEWER
        if uid_int in authorized_ids(cfg, env):
            return AUTHORIZED
    except (ValueError, TypeError):
        pass

    # If no specific role found, use default_role from config
    default_role = _get_default_role(cfg)
    if default_role and default_role in [VIEWER, EDITOR, ADMIN]:
        return default_role

    return None


def has_permission(uid: Optional[Any], permission: str, cfg: Optional[dict], env: Optional[dict] = None,
                   course: Optional[str] = None) -> bool:
    """Check if a user has a specific permission. Supports per-course access control."""
    role = role_of(uid, cfg, env)
    if role is None:
        return False

    # Legacy authorized role gets all permissions (backward compatibility)
    if role == AUTHORIZED:
        return True

    # Check permission for the role
    if permission not in ROLE_PERMISSIONS.get(role, set()):
        return False

    # If course is specified, check per-course access control
    if course is not None and role != ADMIN:
        return has_course_access(uid, course, cfg, env)

    return True


def has_course_access(uid: Optional[Any], course: str, cfg: Optional[dict], env: Optional[dict] = None) -> bool:
    """Check if a user has access to a specific course.

    Checks database for per-course permissions first, falls back to config.
    """
    role = role_of(uid, cfg, env)
    if role is None:
        return False

    # Admins have access to all courses
    if role == ADMIN:
        return True

    # Legacy authorized role has access to all courses
    if role == AUTHORIZED:
        return True

    # Check database for per-course permissions
    course_id = _get_course_id_from_key(course)
    if course_id:
        db_perm = _get_db_course_permissions(uid, course_id)
        if db_perm:
            # Any permission level (view, edit, admin) grants access
            return True

    # Fallback to config-based per-course assignments
    rbac_cfg = ((cfg or {}).get("attendance") or {}).get("rbac", {})
    if not isinstance(rbac_cfg, dict):
        return False

    user_assignments = rbac_cfg.get("user_assignments", {})
    if not isinstance(user_assignments, dict) or not user_assignments:
        # If no per-course assignments are configured, editor role has access to all courses
        return True

    user_key = str(uid).strip().lower() if uid is not None else None
    if user_key is None:
        return False

    for u_key, u_courses in user_assignments.items():
        if str(u_key).strip().lower() == user_key:
            if isinstance(u_courses, (list, tuple)):
                return course in u_courses

    return False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str      # open | ok | unknown | error | insufficient_permission
    role: Optional[str] = None


AUTHORIZED_IDS: set[int] = set()


def update_roles(cfg: Optional[dict] = None, env: Optional[dict] = None) -> set[int]:
    """Update global role cache. Returns all authorized IDs (legacy + new roles)."""
    global AUTHORIZED_IDS
    all_ids = set()
    all_ids.update(authorized_ids(cfg, env))
    for role in [VIEWER, EDITOR, ADMIN]:
        all_ids.update(role_ids(cfg, role, env))
    AUTHORIZED_IDS = all_ids
    return AUTHORIZED_IDS


def decide(uid: Optional[int], level: str, cfg: Optional[dict], env: Optional[dict] = None,
           course: Optional[str] = None) -> Decision:
    """Decide if a user can access a resource at the given level.

    Level can be:
    - "authorized" (legacy: authorized users only)
    - "viewer" (view_attendance, view_audit_log)
    - "editor" (view_attendance, edit_attendance, view_audit_log)
    - "admin" (all permissions)
    - "strict_admin" (admin only, even if no config)
    - A specific permission string (e.g., "edit_attendance")
    """
    configured = allowlist_configured(cfg, env)
    if not configured:
        if level == "strict_admin":
            return Decision(False, "closed")
        return Decision(True, "open")          # nothing configured: today's behaviour

    role = role_of(uid, cfg, env)
    if role is None:
        return Decision(False, "unknown")

    # Map level to permission check
    if level == "authorized":
        # Legacy: any role including authorized
        return Decision(True, "ok", role)

    if level in [VIEWER, EDITOR, ADMIN]:
        # Check if user has this role or higher
        role_hierarchy = {VIEWER: 1, EDITOR: 2, ADMIN: 3}
        user_level = role_hierarchy.get(role, 0)
        required_level = role_hierarchy.get(level, 0)
        if user_level >= required_level:
            return Decision(True, "ok", role)
        return Decision(False, "insufficient_permission", role)

    if level == "strict_admin":
        if role == ADMIN:
            return Decision(True, "ok", role)
        return Decision(False, "insufficient_permission", role)

    # Treat level as a specific permission
    if has_permission(uid, level, cfg, env, course):
        return Decision(True, "ok", role)
    return Decision(False, "insufficient_permission", role)


def startup_warnings(cfg: Optional[dict], env: Optional[dict] = None) -> list[str]:
    """Plain-language warnings for the log at bot start."""
    authorized = authorized_ids(cfg, env)
    rbac_cfg = ((cfg or {}).get("attendance") or {}).get("rbac", {})
    has_rbac = isinstance(rbac_cfg, dict) and rbac_cfg.get("roles")

    if not authorized and not has_rbac:
        return ["No bot allow-list is configured: EVERYONE who finds the bot can use it. "
                f"Set {AUTHORIZED_ENV} or configure RBAC roles and restart to restrict access."]

    warnings = []
    if authorized and has_rbac:
        warnings.append("Both legacy authorized IDs and new RBAC roles are configured. "
                       "RBAC roles will take precedence where applicable.")
    return warnings


# ─── Refusal wording (no course/student data, only the user's own ID) ─────────

def refusal_text(d: Decision, level: str, uid: Optional[int]) -> str:
    if d.reason == "error":
        return "❌ Couldn't verify your access right now. Please try again in a moment."
    if d.reason == "closed":
        return "🔒 This command is unavailable: no authorized user is configured yet."
    if d.reason == "insufficient_permission":
        role_msg = f"Your role is {d.role}" if d.role else "Your role could not be determined"
        return f"🔒 Sorry, you don't have permission for this action.\n{role_msg}. Please contact an admin."
    who = f"Your Telegram ID is {uid}" if uid is not None else "Your Telegram ID could not be read"
    return (f"🔒 Sorry, you're not authorised to use this bot yet.\n"
            f"{who}. Please send it to an admin so they can add you.")


def alert_text(d: Decision, level: str, uid: Optional[int]) -> str:
    """Short text for a callback-query alert (Telegram limit: 200 chars)."""
    if d.reason == "error":
        return "❌ Couldn't verify your access. Try again."
    if d.reason == "closed":
        return "🔒 Command unavailable (no authorized user configured)."
    if d.reason == "insufficient_permission":
        return f"🔒 Insufficient permissions (role: {d.role})."
    return f"🔒 Not authorised. Your Telegram ID: {uid}" if uid is not None else "🔒 Not authorised."


async def _refuse(update, context, d: Decision, level: str, uid: Optional[int]) -> None:
    try:
        query = getattr(update, "callback_query", None)
        if query is not None:
            await query.answer(alert_text(d, level, uid), show_alert=True)   # stops the spinner
            return
        msg = getattr(update, "message", None) or getattr(update, "effective_message", None)
        if msg is not None:
            await msg.reply_text(refusal_text(d, level, uid))
            return
        chat = getattr(update, "effective_chat", None)
        if chat is not None and getattr(context, "bot", None) is not None:
            await context.bot.send_message(chat.id, refusal_text(d, level, uid))
    except Exception:
        pass   # a failed refusal message must never become an access grant or a crash


def guard(level: str, load_cfg: Callable[[], dict], get_log: Optional[Callable[[], object]] = None,
          deny_return=None, course_key: Optional[str] = None):
    """Decorator for Telegram handlers. `load_cfg` / `get_log` are called at request time.

    Args:
        level: Role level (viewer, editor, admin, authorized, strict_admin) or specific permission.
        load_cfg: Function to load config at request time.
        get_log: Optional function to get logger.
        deny_return: Value to return if access denied.
        course_key: Optional key name in context args to extract course for per-course access control.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(update, context, *a, **k):
            uid = None
            course = None
            try:
                uid = getattr(getattr(update, "effective_user", None), "id", None)
                # Extract course from context if course_key is provided
                if course_key and course_key in k:
                    course = k[course_key]
                d = decide(uid, level, load_cfg(), course=course)
            except Exception as exc:
                d = Decision(False, "error")
                try:
                    if get_log:
                        get_log().error("role_check_failed", extra={"error": type(exc).__name__})
                except Exception as exc:
                    log.debug("roles_swallowed", extra={"error": str(exc)})
            if d.allowed:
                return await fn(update, context, *a, **k)
            await _refuse(update, context, d, level, uid)
            return deny_return
        wrapper.required_level = level   # lets tests prove every handler is guarded
        return wrapper
    return deco
