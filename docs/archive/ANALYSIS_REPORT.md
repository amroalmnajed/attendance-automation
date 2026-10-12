# Comprehensive Codebase Analysis

Below is an exhaustive breakdown of the errors, bugs, and design flaws currently present in the system, particularly focusing on the recent regressions.

## 1. Aggressive Noise Filter (Logic Error)
**File**: `attendance/pipeline.py` (line 143)
```python
if not raw_clean or len(raw_clean) < 3 or raw_clean in NOISE_WORDS:
    continue
```
**Impact**: The `len(raw_clean) < 3` check drops valid 2-letter Arabic names (e.g., "مي" [Mai], "طه" [Taha]) from the pipeline entirely. They bypass the review queue and are completely ignored.

## 2. Blocking I/O in Async Telegram Loop
**File**: `attendance/ingest.py` (line 278)
```python
res = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
```
**Impact**: When a user clicks a review queue inline button, the Telegram bot runs the synchronous `attendance.cli` subprocess. Since `subprocess.run` blocks the `asyncio` event loop for several seconds (while it authenticates and writes to Google Sheets), the entire bot becomes unresponsive, dropping or delaying other user interactions ("failing to trigger or route properly").
**Fix**: Must use `await asyncio.get_running_loop().run_in_executor(...)` to wrap the blocking call.

## 3. Cross-Script String Matching Failure (Architectural Flaw)
**File**: `attendance/normalizer.py` & `attendance/matcher.py`
**Impact**: The Gemini OCR prompt requests transliteration, but Gemini frequently hallucinates or ignores this and returns Arabic text in `raw_name` (e.g., `"عبد الرحمن احمد"`). 
When `matcher.py` receives this, it fuzzy matches (using character-level algorithms like `token_set_ratio`) against the English Google Sheet roster. Since there's no character overlap between scripts, the score drops to 0, completely failing the match.
**Fix**: Implement a true Arabic-to-English mapping in `name_variants.yaml` or add a phonetic/transliteration module inside `normalize_for_matching()` to process un-transliterated Arabic strings before they reach `rapidfuzz`.

## 4. Sub-optimal Matcher Weights & Margins
**File**: `config/attendance.yaml`
```yaml
    weights:
      token_sort: 0.1
      token_set: 0.9
```
**Impact**: The user increased `token_set` to `0.9` and set `margin` to `10`. Because `token_set` compares subset words, a student writing "Mohamed Ali" gets a 100% `token_set` score against both "Mohamed Ali Saber" and "Mohamed Ali Hassan". Their final scores will only differ by the `0.1` weight of `token_sort`. This small difference is almost always less than the `10` margin, immediately triggering a `MATCH_AMBIGUOUS` flag for any student with a shared name.

## 5. Early Temporary Image Deletion (Usability Bug)
**File**: `attendance/ingest.py` (line 211)
```python
    finally:
        try:
            os.remove(img_path)
        except OSError:
            pass
```
**Impact**: The image is aggressively deleted the moment the OCR pass completes. However, `review_queue.py` stores `image_ref: str` pointing to this local file. If an admin or developer attempts to view the image to manually resolve a flagged item, they will find the file is missing (`[Errno 2]`), rendering the review process blind.

## 6. Race Condition in Review Queue Modifications
**File**: `attendance/review_queue.py` (lines 118-147)
**Impact**: The `resolve()` method reads the JSON file, alters the item, and overwrites the JSON. If two inline buttons are pressed simultaneously, two concurrent `subprocess.run` calls will execute `resolve()`, resulting in a race condition where one resolution overwrites and deletes the other's status.

## 7. Out-of-Bounds Roster Indexing Vulnerability
**File**: `attendance/pipeline.py` (line 173)
```python
current = col_data[roster_row - 1] if roster_row <= len(col_data) else ""
```
**Impact**: If `roster_row` is evaluated as `0` (e.g., from a faulty resolution or manual test injection), `roster_row - 1` becomes `-1`. Python negative indexing will silently fetch the *last* element of the column data instead of raising an error or returning `""`, potentially overwriting the wrong cell.

## 8. Missing Fallback in Type Button Logic
**File**: `attendance/ingest.py` (lines 110-117)
**Impact**: If a course configuration in `attendance.yaml` declares `layout: typed_pair` but omits the `types` dictionary entirely (or misspells `any`/`odd`/`even`), the `types` list evaluates to empty. The bot will send a message with an empty `InlineKeyboardMarkup`, throwing a Telegram API `BadRequest` error ("reply markup is empty") and breaking the ingestion flow.


---
## Status (2026-10-08, session 3)
| # | Item | Status |
|---|---|---|
| 1 | 2-char noise filter | Fixed (MIN_CHARS=2) |
| 2 | Blocking subprocess in bot | Fixed (run_in_executor) |
| 3 | Arabic cross-script | Mitigated (`_AR_TO_LATIN` deduped; table still partial) |
| 4 | Matcher weights | Fixed (0.5/0.5) |
| 5 | Early image deletion | Open (image deleted in `finally`; `image_ref` dangles) |
| 6 | Review-queue race | Fixed: cross-process `file_lock` + atomic replace (`_fslock.py`); double-resolve raises |
| 7 | Negative roster index | Fixed (guard `roster_row <= 0`) |
| 8 | Empty typed_pair keyboard | Open |
| 9 | NEW: `review resolve` marked item resolved before sheet write (lost writes on failure) | Fixed: write first, then resolve |
| 10 | NEW: `Eldin`/`Aldin`→`El` variant corrupted names like "Salah Eldin"; `-Gidaa` YAML typo; multi-word variants never matched | Fixed |
