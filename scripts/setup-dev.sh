#!/usr/bin/env bash
# One-time dev environment setup: deps + git hooks (gitleaks, whitespace, yaml checks).
set -euo pipefail
cd "$(dirname "$0")/.."
pip install -r requirements-dev.txt
pre-commit install
