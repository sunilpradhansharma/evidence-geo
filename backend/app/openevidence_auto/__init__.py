"""Unattended OpenEvidence auto-capture.

OpenEvidence has no public API and is HCP-gated, so this package automates the real
web UI with Playwright (a seeded, reused login session) and feeds the scraped answers
into the existing manual-capture bridge (app.services.openevidence_service), so they
are scored and folded into Chairman consensus exactly like a human paste would be.

Modules:
- browser.py  -> the Playwright harness (login + ask + scrape).
- worker.py   -> orchestration that reuses capture()/finalize_capture().

Playwright is imported lazily inside browser.py so importing this package (e.g. for
the API/status endpoints) never fails when Playwright/Chromium isn't installed.
"""
