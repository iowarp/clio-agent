"""CLI event consumption using server-owned input and completion identities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.panel import Panel

from clio_agent.client import (
    ClioSDKError,
    MessageCompleted,
    PermissionRequested,
    SessionStatusChanged,
)

if TYPE_CHECKING:
    from clio_agent.ui.cli import ClioAgentCLI


def consume_turn(
    cli: ClioAgentCLI,
    session_id: str,
    ack_message_id: str,
    *,
    interactive: bool = True,
    status: Any = None,
    answered_question: str | None = None,
) -> tuple[Any, Any, dict[str, Any] | None]:
    """Consume completion and human-input events from the session feed.

    Fetch the authoritative assistant on ``message.completed``. Resolve
    ``user_question.created`` (or legacy ``waiting_user``) and permissions,
    then reopen the feed from its cursor. Posting a Question itself does
    not pause model execution; the agent may explicitly wait on its handle.

    Input rounds retain the CLI's MAX_PAUSE_ROUNDS safeguard. Return
    ``(completed_event, assistant_message, error_info)``; consume failures
    remain structured, including unavailable input and failed message reads.
    """
    completed = None
    error_info: dict[str, Any] | None = None
    rounds = 0
    root_answers = [answered_question] if answered_question else []
    child_answers: list[tuple[str, str]] = []
    completions: dict[str, Any] = {}

    while True:
        pause_kind: str | None = None
        pause_permission: Any = None
        question_id: str | None = None
        stream_ended = True
        with cli.client.sessions.events(session_id, last_event_id=cli._event_cursor) as stream:
            for event in stream:
                if isinstance(event, MessageCompleted):
                    identity = str(event.payload.get("turn_id") or "")
                    completions[identity] = event
                    targets = {ack_message_id}
                    if not root_answers and identity != ack_message_id:
                        submitted = cli.client.messages.get(session_id, ack_message_id)
                        metadata = getattr(submitted, "metadata", {})
                        if "recipient_input" in metadata:
                            receipt = metadata["recipient_input"]
                            targets = {str(receipt.get("turn_id") or "") if receipt else ""}
                    if root_answers:
                        targets = set()
                        for handle in dict.fromkeys(root_answers):
                            receipt = cli.client.sessions.question(session_id, handle).answer_input
                            targets.add(str(receipt.get("turn_id") or "") if receipt else "")
                    if "" not in targets and targets <= completions.keys():
                        completed = next(
                            value for key, value in reversed(completions.items()) if key in targets
                        )
                        stream_ended = False
                        break
                    continue  # A prior segment cannot complete this input's continuation.
                if (
                    isinstance(event, SessionStatusChanged)
                    and event.status == "waiting_user"
                    or event.type == "user_question.created"
                ):
                    pause_kind = "waiting_user"
                    question_id = str(event.payload.get("id") or "") or None
                    stream_ended = False
                    break
                if isinstance(event, PermissionRequested):
                    pause_kind = "permission"
                    pause_permission = event.permission
                    stream_ended = False
                    break
            cli._event_cursor = stream.last_event_id

        if completed is not None:
            break
        if pause_kind is None:
            # The feed ended without a terminal event and without a
            # pause — surface it structurally rather than returning a
            # silently empty answer.
            if stream_ended:
                error_info = {
                    "error": "stream_ended",
                    "message": (
                        "The event stream ended before the turn completed; "
                        "the answer could not be retrieved."
                    ),
                }
            break

        rounds += 1
        if rounds > cli.MAX_PAUSE_ROUNDS:
            error_info = {
                "error": "too_many_pauses",
                "message": (
                    f"The turn did not complete after {cli.MAX_PAUSE_ROUNDS} "
                    "clarification/permission rounds; aborting to avoid an "
                    "unbounded wait."
                ),
            }
            break

        if pause_kind == "waiting_user":
            abort, answered = cli._resolve_waiting_user(
                session_id, interactive=interactive, status=status, question_id=question_id
            )
            if answered is not None:
                owner = answered.owner_session_id or answered.session_id
                if owner == session_id:
                    root_answers.append(answered.id)
                else:
                    child_answers.append((owner, answered.id))
        else:
            abort = cli._resolve_permission(
                pause_permission, interactive=interactive, status=status
            )
        if abort is not None:
            error_info = abort
            break
        # Loop back to re-open the feed and consume the resumed turn.

    # Attended child input belongs to that child's receipt and completion
    # feed. Retain the parent's result; do not wait on unrelated descendants.
    for owner, handle in dict.fromkeys(child_answers) if completed is not None else ():
        cursor = cli._event_cursor
        cli._event_cursor = None
        try:
            _child, _message, child_error = cli._consume_turn(
                owner, "", interactive=interactive, status=status, answered_question=handle
            )
        finally:
            cli._event_cursor = cursor
        if child_error is not None and error_info is None:
            error_info = child_error
    message = None
    if completed is not None and completed.message_id:
        try:
            message = cli.client.messages.get(session_id, completed.message_id)
        except ClioSDKError as exc:
            # The turn completed cleanly but the authoritative message
            # fetch failed. Do NOT degrade to an empty answer — surface a
            # structured reason so it reaches the user (no-silent-fallback).
            error_info = {
                "error": "message_fetch_failed",
                "message": (f"The turn completed but its message could not be fetched: {exc}"),
            }
    return completed, message, error_info


def resolve_waiting_user(
    cli: ClioAgentCLI,
    session_id: str,
    *,
    interactive: bool,
    status: Any = None,
    question_id: str | None = None,
) -> tuple[dict[str, Any] | None, Any]:
    """Resolve a ``waiting_user`` pause: fetch the pending question, prompt
    the user, and submit the answer.

    Return an error and the Question actually answered, if any. Its owner
    and confirmed input identify the continuation; answering alone does not.
    """
    pending = (
        [cli.client.sessions.question(session_id, question_id)]
        if question_id
        else cli.client.sessions.questions(session_id, status="pending")
    )
    pending = [question for question in pending if question.status == "pending"]
    if not pending:
        # Already resolved out-of-band — nothing to do; just resume.
        return None, None
    question = pending[0]

    if not interactive:
        return {
            "error": "input_required",
            "message": (
                "The agent asked a clarifying question but the CLI is running "
                "non-interactively and cannot prompt for an answer: "
                f"{question.prompt}"
            ),
        }, None

    bundle = question.metadata.get("question_bundle")
    if isinstance(bundle, dict) and isinstance(bundle.get("questions"), list):
        answers = {}
        for field in bundle["questions"]:
            answer, selected = _prompt_question_field(
                cli,
                str(field["question"]),
                [(str(option["value"]), str(option["label"])) for option in field["options"]],
                allow_freeform=bool(field["allow_freeform"]),
                status=status,
            )
            answers[field["id"]] = {"answer": answer, "selected_options": selected}
        answered = cli.client.sessions.answer_question(
            session_id, question.id, metadata={"answers": answers}
        )
    else:
        answer, selected = _prompt_question_field(
            cli,
            question.prompt,
            [(opt.value or opt.label, opt.label) for opt in question.options],
            allow_freeform=question.allow_freeform,
            status=status,
        )
        if selected:
            answered = cli.client.sessions.answer_question(
                session_id, question.id, answer=answer, selected_options=selected
            )
        else:
            answered = cli.client.sessions.answer_question(session_id, question.id, answer=answer)
    return None, answered


def _prompt_question_field(
    cli: ClioAgentCLI,
    prompt: str,
    options: list[tuple[str, str]],
    *,
    allow_freeform: bool,
    status: Any,
) -> tuple[str, list[str]]:
    """Collect one field, preserving offered option identity and escaping model text."""
    body = f"[bold]{escape(prompt)}[/bold]"
    if options:
        lines = "\n".join(f"  - {escape(value)}: {escape(label)}" for value, label in options)
        body = f"{body}\n\n[dim]Options:[/dim]\n{lines}"
    cli.console.print(
        Panel(body, title="[#FFAA00]CLIO needs your input[/#FFAA00]", border_style="#FFAA00")
    )
    while True:
        answer = cli._prompt("Your answer: ", status=status).strip()
        selected = next((value for value, label in options if answer in {value, label}), None)
        if selected is not None:
            return "", [selected]
        if answer and allow_freeform:
            return answer, []
        cli.console.print("Choose an offered option." if options else "An answer is required.")
