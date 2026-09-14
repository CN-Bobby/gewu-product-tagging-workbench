"""Agent backend contract: the only thing AgentBridge is allowed to know about an agent runtime.

A backend owns its own transport (a CLI, an HTTP API, an in-process double) and reports through
one sink: lifecycle/diagnostic events and agent output events. The bridge never reads a process
stream and never parses a wire format; those belong to a concrete adapter such as a Reasonix,
Codex or Qoder adapter, or the fake backend the tests use.

Event types a backend may report through the sink:
  assistant_text_delta, turn_settled,
  tool_call_started, tool_call_settled
Anything else is treated as a contract violation and recorded as backend_event_ignored.

``turn_settled`` reports the turn's terminal outcome and nothing else: it does not claim the
session is healthy, the tools succeeded or the work is done. Tool events likewise describe the
host's own lifecycle for one invocation, never the tool's physical execution.
"""
from typing import Callable, Protocol, runtime_checkable

EventSink=Callable[[dict],None]


class BackendError(Exception):
    """Base for every failure a backend declares. ``fatal`` is the only thing the bridge reads
    to decide whether the session survives, so no caller has to inspect text or codes."""
    fatal=False
    def __init__(self,message='',detail=None):
        self.detail=detail
        super().__init__(message or type(self).__name__)


class TurnError(BackendError):
    """One turn failed. The session and its runtime are still usable."""


class CancelledError(BackendError):  # noqa: A001 - the contract name, not asyncio's
    """The turn was cancelled on request. The session stays active."""


class RuntimeUnavailable(BackendError):
    """The backend can no longer do any work; the session must be rebuilt or recovered."""
    fatal=True


@runtime_checkable
class AgentBackend(Protocol):
    """Minimal capability set. One backend instance serves exactly one agent session."""

    def bind(self,emit:EventSink) -> None:
        """Register the sink every event is reported through. Called once, before start()."""

    def start(self) -> None:
        """Bring the runtime up. Raise on failure; the caller turns that into a session failure."""

    def send(self,text:str) -> None:
        """Run one turn. Report deltas and completion through the sink before returning.
        Declared failures use BackendError subclasses so the caller knows if it is fatal."""

    def cancel(self) -> None:
        """Abort the turn in flight without discarding the backend session."""

    def answer_question(self,interaction_id:str,option_id:str) -> bool:
        """Answer a pending question by a backend-neutral interaction id. The backend decides
        whether the answer is valid; callers never learn how this backend addresses its peer.

        ``True`` means exactly one thing: the interaction was claimed and the protocol reply was
        handed to the peer. It does not mean the settlement event was delivered, and it does not
        mean the surrounding turn succeeded — a settlement that never reached the log surfaces as
        the turn's own failure, never as a second meaning for this flag."""

    def cancel_question(self,interaction_id:str) -> bool:
        """Refuse a pending question, with the same meaning of ``True`` as ``answer_question``.
        The turn's outcome is the peer's to report, not ours."""

    def answer_permission(self,interaction_id:str,option_id:str) -> bool:
        """Answer a pending permission request, with the same meaning of ``True`` as
        ``answer_question``. An answer is not a claim about what a tool did."""

    def cancel_permission(self,interaction_id:str) -> bool:
        """Refuse a pending permission request, with the same meaning of ``True`` as
        ``answer_question``."""

    def stop(self) -> None:
        """Release the runtime. Must tolerate being called more than once."""

    def status(self) -> dict:
        """Read-only backend facts, such as the runtime's own remote session id."""
