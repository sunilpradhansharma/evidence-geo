"""Cooperative + preemptive run cancellation registry (in-process).

Runs execute as background tasks in the API process. Cancellation works two ways:
- a polled flag (`is_cancel_requested`) the orchestrator checks between questions and
  between provider retries (cheap, no await), and
- a per-run `asyncio.Event` (`register_run`/`get_cancel_event`) the orchestrator can
  *await*, so a cancel request preempts in-flight LLM calls immediately (NF-005)
  instead of waiting for the current question's whole batch to finish.

Single-process / single-worker assumption: the cancel endpoint and the background run
task share one event loop, so `request_cancel` can set the Event directly.
"""
import asyncio

_cancel_requested: set[str] = set()
_cancel_events: dict[str, asyncio.Event] = {}


def request_cancel(run_id: str) -> None:
    """Flag a run for cancellation and wake the orchestrator if it is awaiting."""
    _cancel_requested.add(run_id)
    event = _cancel_events.get(run_id)
    if event is not None:
        event.set()


def is_cancel_requested(run_id: str) -> bool:
    return run_id in _cancel_requested


def register_run(run_id: str) -> asyncio.Event:
    """Create (or fetch) the cancel Event for a run. Called by the orchestrator at the
    start of execution so a later `request_cancel` can preempt it. If a cancel was
    already requested before registration, the returned Event is already set."""
    event = _cancel_events.get(run_id)
    if event is None:
        event = asyncio.Event()
        _cancel_events[run_id] = event
    if run_id in _cancel_requested:
        event.set()
    return event


def get_cancel_event(run_id: str) -> asyncio.Event | None:
    return _cancel_events.get(run_id)


def clear_cancel(run_id: str) -> None:
    _cancel_requested.discard(run_id)
    _cancel_events.pop(run_id, None)
