"""webapp/pages/admin.py — Admin tools (W7).

Stub screens for revert, absent-offer, alias management, and health status.
Clearly labeled as "not yet ported" per W7 spec.
"""
from __future__ import annotations

import streamlit as st


def _page_admin() -> None:
    """Render the admin tools screen."""
    st.header("🛠️ Admin Tools")

    st.warning(
        "⚠️ **Most admin tools are not yet ported from the CLI/bot.** "
        "Use the CLI or Telegram bot for these features until they are implemented here."
    )

    # Tabs for each admin tool
    tab1, tab2, tab3, tab4 = st.tabs(["Revert", "Absent Offer", "Aliases", "Health"])

    with tab1:
        _render_revert_stub()

    with tab2:
        _render_absent_offer_stub()

    with tab3:
        _render_aliases_stub()

    with tab4:
        _render_health_stub()


def _render_revert_stub() -> None:
    """Render the revert tool stub (not yet ported)."""
    st.subheader("🔄 Revert")

    st.info(
        """
        **Not yet ported.**

        This tool will allow you to revert a previous write operation
        (equivalent to the CLI `/revert` command).

        Until this is implemented, use:
        ```bash
        python -m attendance.cli revert --upload-id <upload_id> --item-id <item_id>
        ```
        """
    )


def _render_absent_offer_stub() -> None:
    """Render the absent-marking offer stub (not yet ported)."""
    st.subheader("📢 Absent-Marking Offer")

    st.info(
        """
        **Not yet ported.**

        This tool will allow you to mark missing students as absent
        after an upload (equivalent to the bot's absent button).

        The absent offer is surfaced in the upload summary when
        `summary.absent_offer` is True.

        Until this is implemented, use the CLI or check the upload
        summary for the `summary.missing_count` and manually mark
        students absent.
        """
    )


def _render_aliases_stub() -> None:
    """Render the alias management stub (not yet ported)."""
    st.subheader("📚 Alias Management")

    st.info(
        """
        **Not yet ported.**

        This tool will allow you to manage learned aliases
        (equivalent to the CLI `/aliases` command and bot's alias management).

        Until this is implemented, use:
        ```bash
        python -m attendance.cli aliases list
        python -m attendance.cli aliases suggest
        ```
        """
    )


def _render_health_stub() -> None:
    """Render the health status stub (not yet ported)."""
    st.subheader("🏥 Health Status")

    st.info(
        """
        **Not yet ported.**

        This tool will display system health status
        (equivalent to the CLI `/health` command and bot's health checks).

        Until this is implemented, use:
        ```bash
        python -m attendance.cli health
        ```
        """
    )
