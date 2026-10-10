# Product Guidelines

## Voice & Tone
- **Professional but Approachable:** Communications (via the Telegram bot or command line) should be clear, concise, and helpful. Avoid overly technical jargon when interacting with teaching staff.
- **Direct & Action-Oriented:** Use active voice. When errors occur or manual review is needed, provide clear, actionable steps for the user to resolve the issue.
- **Empathetic & Assuring:** The system handles sensitive data (attendance). Assure the user that the system is reliable and that changes can be reverted if mistakes are made.

## UX Principles
- **Frictionless Ingestion:** The primary workflow (uploading a photo) should require zero configuration and minimal button taps. Smart defaults should be used wherever possible.
- **Human-in-the-Loop Transparency:** When the system is unsure (e.g., low OCR confidence), it must fail safely by deferring to human review rather than guessing incorrectly.
- **Clear Feedback Loops:** Every action (upload, review resolution, revert) must provide immediate, clear confirmation of success or failure.
- **Resilience:** The bot must remain responsive and informative even when external services (Google Sheets, Gemini OCR) encounter transient errors.
