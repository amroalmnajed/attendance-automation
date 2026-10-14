"""tests/test_zoom_api.py — Tests for Zoom API integration (mocked)."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from attendance.zoom_api import (
    fetch_past_meeting_participants,
    get_oauth_token,
    list_recent_meeting_instances,
)


# ─── OAuth token tests ─────────────────────────────────────────────────────────

@patch.dict("os.environ", {"ZOOM_ACCOUNT_ID": "test_account", "ZOOM_CLIENT_ID": "test_client", "ZOOM_CLIENT_SECRET": "test_secret"})
@patch("attendance.zoom_api.httpx.post")
def test_get_oauth_token_success(mock_post):
    """Test successful OAuth token fetch."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "test_token_123",
        "expires_in": 3600,
    }
    mock_post.return_value = mock_response

    config = {
        "account_id_env": "ZOOM_ACCOUNT_ID",
        "client_id_env": "ZOOM_CLIENT_ID",
        "client_secret_env": "ZOOM_CLIENT_SECRET",
        "token_url": "https://zoom.us/oauth/token",
    }

    token = get_oauth_token(config)
    assert token == "test_token_123"
    mock_post.assert_called_once()


@patch.dict("os.environ", {"ZOOM_ACCOUNT_ID": "test_account", "ZOOM_CLIENT_ID": "test_client", "ZOOM_CLIENT_SECRET": "test_secret"})
@patch("attendance.zoom_api.httpx.post")
def test_get_oauth_token_cache(mock_post):
    """Test OAuth token caching."""
    from attendance import zoom_api
    zoom_api._token_cache.clear()  # Clear cache before test

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "test_token_123",
        "expires_in": 3600,
    }
    mock_post.return_value = mock_response

    config = {
        "account_id_env": "ZOOM_ACCOUNT_ID",
        "client_id_env": "ZOOM_CLIENT_ID",
        "client_secret_env": "ZOOM_CLIENT_SECRET",
        "token_url": "https://zoom.us/oauth/token",
    }

    # First call
    token1 = get_oauth_token(config)
    assert token1 == "test_token_123"
    assert mock_post.call_count == 1

    # Second call should use cache
    token2 = get_oauth_token(config)
    assert token2 == "test_token_123"
    assert mock_post.call_count == 1  # No additional call


@patch.dict("os.environ", {})
def test_get_oauth_token_missing_credentials():
    """Test OAuth token fetch with missing credentials."""
    config = {
        "account_id_env": "ZOOM_ACCOUNT_ID",
        "client_id_env": "ZOOM_CLIENT_ID",
        "client_secret_env": "ZOOM_CLIENT_SECRET",
    }

    with pytest.raises(RuntimeError, match="Zoom API credentials not found"):
        get_oauth_token(config)


@patch.dict("os.environ", {"ZOOM_ACCOUNT_ID": "test_account", "ZOOM_CLIENT_ID": "test_client", "ZOOM_CLIENT_SECRET": "test_secret"})
@patch("attendance.zoom_api.httpx.post")
def test_get_oauth_token_http_error(mock_post):
    """Test OAuth token fetch with HTTP error."""
    from attendance import zoom_api
    zoom_api._token_cache.clear()  # Clear cache before test

    import httpx
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    mock_post.return_value = mock_response
    mock_post.return_value.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=mock_response
    ))

    config = {
        "account_id_env": "ZOOM_ACCOUNT_ID",
        "client_id_env": "ZOOM_CLIENT_ID",
        "client_secret_env": "ZOOM_CLIENT_SECRET",
        "token_url": "https://zoom.us/oauth/token",
    }

    with pytest.raises(RuntimeError, match="Zoom API token fetch failed"):
        get_oauth_token(config)


# ─── Participant report tests ───────────────────────────────────────────────────

@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_fetch_past_meeting_participants_success(mock_get, mock_token):
    """Test successful participant report fetch."""
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "participants": [
            {
                "name": "Yaman",
                "email": "yaman@example.com",
                "join_time": "2026-09-14T10:00:00Z",
                "leave_time": "2026-09-14T11:30:00Z",
                "duration": 90,
                "guest": "false",
            },
            {
                "name": "Ahmed Ali",
                "email": "ahmed@example.com",
                "join_time": "2026-09-14T10:05:00Z",
                "leave_time": "2026-09-14T11:00:00Z",
                "duration": 55,
                "guest": "false",
            },
        ],
        "next_page_token": "",  # no more pages
    }
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    participants = fetch_past_meeting_participants("81795786552", None, config)

    assert len(participants) == 2
    assert participants[0]["name"] == "Yaman"
    assert participants[0]["email"] == "yaman@example.com"
    assert participants[0]["duration_minutes"] == 90
    assert participants[0]["guest"] is False
    assert participants[1]["name"] == "Ahmed Ali"


@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_fetch_past_meeting_participants_with_instance(mock_get, mock_token):
    """Test participant report fetch with specific instance UUID.

    NOTE: Zoom API does not support instance-specific participant endpoints —
    the instance_uuid parameter is intentionally ignored and the base
    /past_meetings/{id}/participants endpoint is used instead.
    """
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "participants": [
            {
                "name": "Yaman",
                "join_time": "2026-09-14T10:00:00Z",
                "leave_time": "2026-09-14T11:30:00Z",
                "duration": 90,
                "guest": "false",
            },
        ],
        "next_page_token": "",  # no more pages
    }
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    participants = fetch_past_meeting_participants("81795786552", "uuid123", config)

    assert len(participants) == 1
    assert participants[0]["name"] == "Yaman"
    # Verify the correct base URL is used (instance UUID is intentionally ignored
    # because Zoom's /instances/{uuid}/participants endpoint returns 404)
    call_args = mock_get.call_args
    assert "past_meetings/81795786552/participants" in call_args[0][0]
    assert "instances" not in call_args[0][0]


@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_fetch_past_meeting_participants_not_found(mock_get, mock_token):
    """Test participant report fetch with meeting not found."""
    import httpx
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "404 Not Found", request=MagicMock(), response=mock_response
    ))
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    with pytest.raises(RuntimeError, match="Meeting not found"):
        fetch_past_meeting_participants("81795786552", None, config)


@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_fetch_past_meeting_participants_not_ready(mock_get, mock_token):
    """Test participant report fetch when report not ready yet."""
    import httpx
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "400 Bad Request", request=MagicMock(), response=mock_response
    ))
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    with pytest.raises(RuntimeError, match="Participant report error"):
        fetch_past_meeting_participants("81795786552", None, config)


@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_fetch_past_meeting_participants_auth_failure(mock_get, mock_token):
    """Test participant report fetch with auth failure."""
    import httpx
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=mock_response
    ))
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    with pytest.raises(RuntimeError, match="Zoom API failed"):
        fetch_past_meeting_participants("81795786552", None, config)


# ─── Instance listing tests ─────────────────────────────────────────────────────

@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_list_recent_meeting_instances_success(mock_get, mock_token):
    """Test successful instance listing."""
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "meetings": [
            {
                "uuid": "uuid1",
                "start_time": "2026-09-14T10:00:00Z",
                "topic": "Weekly Meeting",
            },
            {
                "uuid": "uuid2",
                "start_time": "2026-09-21T10:00:00Z",
                "topic": "Weekly Meeting",
            },
        ]
    }
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    instances = list_recent_meeting_instances("81795786552", config)

    assert len(instances) == 2
    assert instances[0]["uuid"] == "uuid1"
    assert instances[0]["start_time"] == "2026-09-14T10:00:00Z"
    assert instances[1]["uuid"] == "uuid2"


@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_list_recent_meeting_instances_not_found(mock_get, mock_token):
    """Test instance listing with meeting not found."""
    import httpx
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "404 Not Found", request=MagicMock(), response=mock_response
    ))
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    with pytest.raises(RuntimeError, match="Meeting not found"):
        list_recent_meeting_instances("81795786552", config)


@patch("attendance.zoom_api.get_oauth_token")
@patch("attendance.zoom_api.httpx.get")
def test_list_recent_meeting_instances_auth_failure(mock_get, mock_token):
    """Test instance listing with auth failure."""
    import httpx
    mock_token.return_value = "test_token"

    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"
    mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "403 Forbidden", request=MagicMock(), response=mock_response
    ))
    mock_get.return_value = mock_response

    config = {
        "api_base_url": "https://api.zoom.us/v2",
    }

    with pytest.raises(RuntimeError, match="Zoom API failed"):
        list_recent_meeting_instances("81795786552", config)
