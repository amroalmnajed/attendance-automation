What's in this folder
======================
These are the 4 files I changed in your attendance-automation project, with the
same folder structure (attendance/..., config/...). Nothing else was touched —
your .env, service_account.json, review_queue, logs, etc. are untouched and NOT
included here, so this download has no secrets in it.

How to apply
============
Copy these files into your project, overwriting the originals at the same paths:
  attendance/ocr.py          -> C:\...\attendance-automation\attendance\ocr.py
  attendance/matcher.py      -> C:\...\attendance-automation\attendance\matcher.py
  attendance/pipeline.py     -> C:\...\attendance-automation\attendance\pipeline.py
  attendance/ingest.py       -> C:\...\attendance-automation\attendance\ingest.py
  config/attendance.yaml     -> C:\...\attendance-automation\config\attendance.yaml

Then just run the bot as usual (start_bot.bat / python -m attendance.ingest).
No new dependencies, no .env changes needed.

What changed and why
=====================
1. attendance/ingest.py
   - Fixed the crash that was killing every review-queue send: it read
     item['confidence'] but review items are stored under 'ocr_confidence'.
     This is the exact error in your logs.jsonl ("ingest_pipeline_error: 'confidence'").
   - Fixed "Tuesday online" photo-caption shortcut writing a single-space type
     string when your sheet's real dropdown value has a double space
     ("Tuesday  - Online"), which would have failed sheet validation silently.

2. THE MAIN FIX — attendance/ocr.py, attendance/matcher.py, attendance/pipeline.py,
   config/attendance.yaml:
   This is the "no brain" problem. Previously, reading the photo and matching
   names against your roster were two blind, disconnected steps:
     - Gemini read the photo with ZERO knowledge of your actual student list,
       and just guessed a transliteration of whatever it saw.
     - Then a plain string-similarity algorithm (RapidFuzz) tried to guess
       which roster student that guess was closest to.
   That's why partial/short names on paper (e.g. "Abdelrahman Ahmed") couldn't
   be resolved — three students start with "Abdelrahman ...", and plain
   character-distance genuinely cannot tell them apart. There's even a config
   flag `send_roster_to_provider` that was sitting there turned OFF and never
   wired to anything — a previous attempt clearly intended this fix and
   never finished it.

   Now: the real roster is sent to Gemini ALONG WITH the photo, so it reads
   the handwriting with full knowledge of who's actually in the class — the
   same way I was able to correctly match your attendance sheet photo to your
   roster earlier in this chat by looking at both together. Gemini returns
   its own best-guess roster match + confidence per row. The matcher then
   uses that to break ties the string-distance math alone can't, while still
   running every existing safety gate (score floor, margin, roster-collision
   check, review queue for anything uncertain) — so it can't force a wrong
   write, it just gets a lot better at knowing which name is right, and sends
   far fewer items to the review queue.

   This is turned on by setting `send_roster_to_provider: true` in
   config/attendance.yaml (already done in the file included here).

Verified
========
- All 4 files compile cleanly (py_compile).
- Ran your existing test suite structure by hand against the matcher logic
  with the real ambiguous case from your own review_queue/153c976a4736586c.json
  ("Abdelrahman Ahmed" vs 3 similar roster names): without the fix the top
  guess was wrong (Abdelrahman Noureldin Derham); with the fix it correctly
  picks Abdelrahman Ahmos Mohamed Kamel with a much wider lead.
- I could not run a live Gemini API call from here (no photo of a full
  sheet was in the zip, only pre-cropped single-row review images, and this
  environment can't reach the Gemini API) — so please do one real dry-run
  test with an actual photo before trusting it on a real session. That's the
  one thing I genuinely can't verify from my side.
