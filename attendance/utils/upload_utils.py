"""Upload ID generation utilities.

Generates deterministic IDs from image bytes, course, and session.
"""
import hashlib


def generate_upload_id(image_bytes: bytes, course: str, session_num: int) -> str:
    """Generate deterministic upload ID from image bytes, course, and session number."""
    h = hashlib.sha256(image_bytes + course.encode() + str(session_num).encode())
    return h.hexdigest()[:16]


def generate_text_upload_id(names: list[str], marks: list | None, course: str, session_num: int) -> str:
    """Generate deterministic upload ID for text-input workflow."""
    id_basis = str(names)
    if marks and any((m or "Present") != "Present" for m in marks):
        id_basis += str(list(marks))
    return hashlib.sha256(id_basis.encode() + course.encode() + str(session_num).encode()).hexdigest()[:16]


def generate_multi_upload_id(image_bytes_list: list[bytes], course: str, session_num: int) -> str:
    """Generate deterministic upload ID for multi-image workflow."""
    return hashlib.sha256(b"".join(image_bytes_list) + course.encode() + str(session_num).encode()).hexdigest()[:16]
