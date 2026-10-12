# Telegram Bot Analysis (`attendance/ingest.py`)

During the detailed review of the Telegram bot code (`attendance/ingest.py`), the following potential issues and errors were identified. *No actions have been taken to fix these yet, as requested.*

## 1. Race Condition / Message Not Found Error in Upload Flow
- **Location:** `handle_upload_callback` (lines 151-152) and `_run_pipeline_flow` (line 161).
- **Issue:** When the user clicks "Start Processing", the bot calls `await query.message.delete()` and then immediately schedules `_run_pipeline_flow`, passing the deleted `query.message`. Inside `_run_pipeline_flow`, the bot attempts to reply to that exact message: `await message.reply_text("⏳ Processing sheet...")`. Since the message was just deleted, Telegram will reject this with a `BadRequest: Reply message not found` error, causing the pipeline task to crash before it even begins.

## 2. Blocking File I/O in Async Event Loop
- **Location:** `_send_next_review_item` (line 262) and `handle_resolution_callback` (line 290, 301).
- **Issue:** 
  - Opening the image file `with open(crop_path, "rb") as f:` performs blocking I/O on the main async thread. 
  - `queue.list_pending(full_upload_id)` and directory globbing `Path(review_dir).glob(...)` perform synchronous filesystem operations. In a high-concurrency bot, this blocks the entire `asyncio` event loop. It should be wrapped in `run_in_executor` or `aiofiles`.

## 3. Callback Data Limit Constraints
- **Location:** `_send_next_review_item` (lines 248).
- **Issue:** Telegram enforces a strict 64-byte limit on `callback_data`. The constructed string is `res|{short_upload_id}|{item_id}|{roster_row}`. If `item_id` is a 32-character UUID, this string can reach 50-55 characters. It's dangerously close to the limit and could cause `BadRequest` if `roster_row` or `item_id` lengths increase.

## 4. Conversation Handler State Trap
- **Location:** `search_input_handler` (lines 373-422).
- **Issue:** If a user clicks the "Search" button, the bot enters the `SEARCH_ROSTER` state. If the user fails to find the student and simply sends another photo instead of typing `/cancel`, the bot will ignore the photo because `handle_photo` is not an entry point or valid handler in the `SEARCH_ROSTER` state. The user becomes stuck.

## 5. Potential Uncaught Exceptions in Command Arguments
- **Location:** `cmd_report`, `cmd_streaks`
- **Issue:** The commands assume valid types. For example, `int(parts[2])` in `cmd_report` (line 430) will raise a `ValueError` if the user types `/report g10 text`. Although there is a broad `except Exception as e:` block, relying on it for input validation leads to poor UX (a generic "❌ Error: invalid literal for int()" message).

## 6. Typo in Hardcoded Session Type
- **Location:** `handle_photo` (line 97) and `handle_upload_callback` (line 142).
- **Issue:** The type is written as `"Tuesday  - Online"` with a double space. If the Google Sheet dropdown expects `"Tuesday - Online"` (single space), this will cause a `VALUE_NOT_ALLOWED` or `TYPE_INVALID` error during pipeline resolution.

## 7. File Deletion Failure Swallowed Silently
- **Location:** `_run_pipeline_flow` (line 211-212)
- **Issue:** If `os.remove(img_path)` fails (e.g., due to file locks by OpenCV), the `OSError` is silently swallowed `except OSError: pass`. This could lead to gradual disk space exhaustion in the temporary directory over time.
