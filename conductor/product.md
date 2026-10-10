# Attendance Automation System

## Vision
An automated attendance tracking system designed for an IGCSE tutoring center. It ingests photos of handwritten attendance sheets via a Telegram bot, processes them using Google Gemini OCR to extract student names (in English and Arabic) and attendance marks. The system normalizes and matches the extracted names against a live Google Sheets roster using fuzzy matching and learned aliases, then automatically records the attendance back into the sheet. Ambiguous or low-confidence readings are sent to a manual review queue, which administrators can resolve directly through the Telegram bot.

## Core Value Proposition
- **Efficiency:** Significantly reduces the manual labor required to transcribe handwritten attendance records into digital spreadsheets.
- **Accuracy:** Leverages OCR, fuzzy matching, and human-in-the-loop review queues to ensure high-fidelity data entry.
- **Accessibility:** Enables remote and on-site staff to submit attendance and resolve ambiguities entirely through a familiar Telegram interface.

## Target Audience
- **Teachers & Teaching Assistants:** Need a fast, low-friction way to capture class attendance.
- **Center Administrators:** Need accurate, up-to-date attendance records for reporting and following up with chronic absentees.
