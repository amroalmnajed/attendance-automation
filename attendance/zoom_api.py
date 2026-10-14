"""attendance/zoom_api.py — Zoom API integration for fetching participant reports.

Handles Server-to-Server OAuth authentication and participant report fetching.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any, Optional

import httpx

from core.logger import get_logger

log = get_logger("zoom_api")


# ─── OAuth token management ─────────────────────────────────────────────────────

_token_cache: dict[str, dict[str, Any]] = {}


def get_oauth_token(config: dict) -> str:
    """Fetch and cache Server-to-Server OAuth token with expiry handling.

    Args:
        config: Zoom API configuration dict from attendance.zoom_csv.zoom_api

    Returns:
        Access token string

    Raises:
        RuntimeError: If token fetch fails or credentials are missing
    """
    account_id = os.getenv(config.get("account_id_env", "ZOOM_ACCOUNT_ID"))
    client_id = os.getenv(config.get("client_id_env", "ZOOM_CLIENT_ID"))
    client_secret = os.getenv(config.get("client_secret_env", "ZOOM_CLIENT_SECRET"))
    token_url = config.get("token_url", "https://zoom.us/oauth/token")

    if not all([account_id, client_id, client_secret]):
        raise RuntimeError("Zoom API credentials not found in environment variables")

    cache_key = f"{account_id}:{client_id}"
    cached = _token_cache.get(cache_key)

    # Return cached token if still valid (with 5min buffer)
    if cached and cached.get("expires_at", 0) > time.time() + 300:
        log.debug("zoom_token_cached")
        return cached["token"]

    # Fetch new token
    auth_string = f"{client_id}:{client_secret}"
    b64_auth = base64.b64encode(auth_string.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "account_credentials",
        "account_id": account_id,
    }

    try:
        response = httpx.post(token_url, headers=headers, data=data, timeout=30)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 3600)

        if not access_token:
            raise RuntimeError("Zoom API response missing access_token")

        # Cache token with expiry
        _token_cache[cache_key] = {
            "token": access_token,
            "expires_at": time.time() + expires_in,
        }

        log.info("zoom_token_fetched", extra={"expires_in": expires_in})
        return access_token

    except httpx.HTTPStatusError as e:
        log.error("zoom_token_http_error", extra={"status": e.response.status_code, "text": e.response.text})
        raise RuntimeError(f"Zoom API token fetch failed: {e.response.status_code}") from e
    except Exception as e:
        log.error("zoom_token_error", extra={"error": str(e)})
        raise RuntimeError(f"Zoom API token fetch failed: {e}") from e


# ─── Participant report fetching ────────────────────────────────────────────────

def fetch_past_meeting_participants(
    meeting_id: str,
    instance_uuid: Optional[str],
    config: dict,
) -> list[dict[str, Any]]:
    """Fetch participant report for a past meeting instance.

    Args:
        meeting_id: Zoom meeting ID
        instance_uuid: Optional UUID for specific instance (for recurring meetings)
        config: Zoom API configuration dict

    Returns:
        List of participant records with normalized fields:
        - name (str)
        - email (str or None)
        - join_time (datetime)
        - leave_time (datetime)
        - duration_minutes (float)
        - guest (bool)

    Raises:
        RuntimeError: If API call fails or report not ready
    """
    api_base = config.get("api_base_url", "https://api.zoom.us/v2")
    token = get_oauth_token(config)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Build endpoint URL
    if instance_uuid:
        url = f"{api_base}/past_meetings/{meeting_id}/instances/{instance_uuid}/participants"
    else:
        url = f"{api_base}/past_meetings/{meeting_id}/participants"

    try:
        response = httpx.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        participants = data.get("participants", [])
        log.info("zoom_participants_fetched", extra={"meeting_id": meeting_id, "count": len(participants)})

        # Normalize participant data
        normalized = []
        for p in participants:
            normalized.append({
                "name": p.get("name", ""),
                "email": p.get("email"),
                "join_time": p.get("join_time"),
                "leave_time": p.get("leave_time"),
                "duration_minutes": p.get("duration", 0),
                "guest": p.get("guest", "") == "true",
            })

        return normalized

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise RuntimeError(f"Meeting not found: {meeting_id}") from e
        elif e.response.status_code == 400:
            # Report not ready yet
            raise RuntimeError("Participant report not ready yet. Try again in a few minutes.") from e
        else:
            log.error("zoom_participants_http_error", extra={"status": e.response.status_code, "text": e.response.text})
            raise RuntimeError(f"Zoom API failed: {e.response.status_code}") from e
    except Exception as e:
        log.error("zoom_participants_error", extra={"error": str(e)})
        raise RuntimeError(f"Failed to fetch participants: {e}") from e


def list_recent_meeting_instances(
    meeting_id: str,
    config: dict,
) -> list[dict[str, Any]]:
    """List recent instances for a recurring meeting.

    Args:
        meeting_id: Zoom meeting ID
        config: Zoom API configuration dict

    Returns:
        List of instance records with:
        - uuid (str)
        - start_time (datetime string)
        - topic (str)

    Raises:
        RuntimeError: If API call fails
    """
    api_base = config.get("api_base_url", "https://api.zoom.us/v2")
    token = get_oauth_token(config)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    url = f"{api_base}/past_meetings/{meeting_id}/instances"

    try:
        response = httpx.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        instances = data.get("meetings", [])
        log.info("zoom_instances_listed", extra={"meeting_id": meeting_id, "count": len(instances)})

        # Normalize instance data
        normalized = []
        for inst in instances:
            normalized.append({
                "uuid": inst.get("uuid"),
                "start_time": inst.get("start_time"),
                "topic": inst.get("topic", ""),
            })

        return normalized

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise RuntimeError(f"Meeting not found: {meeting_id}") from e
        else:
            log.error("zoom_instances_http_error", extra={"status": e.response.status_code, "text": e.response.text})
            raise RuntimeError(f"Zoom API failed: {e.response.status_code}") from e
    except Exception as e:
        log.error("zoom_instances_error", extra={"error": str(e)})
        raise RuntimeError(f"Failed to list instances: {e}") from e
