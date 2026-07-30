"""Copilot — the application-wide assistant agent.

A LangGraph ReAct agent (Router -> Orchestrator <-> tool_executor / Analyst
-> Validator) backed by AWS Bedrock (Converse API) that can answer how-to and
data questions about the Evidence Monitoring Agent AND take confirmed write
actions (start runs, harvest, schedule, score overrides, etc.) across every
page of the app.

This package is intentionally separate from ``app.agent`` (which is the
monitoring-run orchestrator) to avoid any naming clash.
"""
