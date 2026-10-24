"""Progress stage constants and callback helpers.

Used by CLI and Streamlit to report workflow progress.
"""
from typing import Callable, Optional

STAGE_READING = "reading"
STAGE_MATCHING = "matching"
STAGE_SAVING = "saving"
ProgressFn = Callable[[str], None]


def _emit(progress: Optional[ProgressFn], stage: str) -> None:
    """Report a stage; a broken callback must never break the workflow."""
    if progress is None:
        return
    try:
        progress(stage)
    except Exception:
        from core.logger import get_logger
        log = get_logger("progress")
        log.warning("progress_callback_failed", extra={"stage": stage})
