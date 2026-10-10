# Technology Stack

## Core Language
- **Python 3:** Primary language for all backend processing, automation, and integration logic.

## Key Libraries & Integrations
- **python-telegram-bot:** Powers the conversational interface for users to upload sheets and manually review ambiguous OCR results.
- **google-genai:** Interfaces with the Gemini Flash model to perform multimodal OCR on handwritten attendance sheets.
- **gspread & google-auth:** Manages authenticated read/write operations against the live Google Sheets roster.
- **opencv-python-headless & Pillow:** Handles image preprocessing (orientation, deskewing, contrast adjustment, cropping) before OCR ingestion.
- **rapidfuzz:** Performs high-performance fuzzy string matching to align raw OCR names with canonical roster names.

## Testing & Quality Assurance
- **pytest:** Primary testing framework for unit and integration tests.
