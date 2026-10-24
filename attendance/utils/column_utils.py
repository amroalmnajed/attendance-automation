"""Column letter to index conversion utilities.

Shared across sheets operations, review queue, and workflows.
"""


def col_letter_to_index(col: str) -> int:
    """'A' → 1, 'B' → 2, 'AA' → 27, etc. (1-based)"""
    result = 0
    for ch in col.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result


def index_to_col_letter(idx: int) -> str:
    """1 → 'A', 27 → 'AA', etc."""
    result = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(rem + ord("A")) + result
    return result
