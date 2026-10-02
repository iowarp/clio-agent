"""LM Studio model load for ``PUT /v1/providers/lm``: reuse or load, never silently (#1577).

Kept out of ``gact/routes/providers.py`` (a baselined god-file). Two steps:

* :func:`loaded_instance_matching` asks LM Studio whether the model is already loaded
  with the requested context length and concurrency. A slow answer is retried with a
  longer bound (:data:`LIST_TIMEOUTS_S`) and, if LM Studio never answers, the bind fails
  typed (:data:`REASON_LIST_UNANSWERED`) -- it no longer reads as "not loaded" and
  silently reloads the model. A refused or unreadable listing is logged with a typed
  reason before the load proceeds.
* :func:`load_model` sends the load. LM Studio's REST API reports no load progress, so the
  wait is bounded by LM Studio staying responsive: while the load is pending its model
  list is polled every :data:`LOAD_POLL_S`; a server that stops answering for
  :data:`LOAD_UNRESPONSIVE_S` fails typed (:data:`REASON_UNRESPONSIVE_DURING_LOAD`), and a
  responsive server that never finishes the load fails typed at :data:`LOAD_CEILING_S`.
  A big model loading from a slow disk is waited for; there is no flat 180 s cut.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Bounds for the "already loaded?" listing: each timeout retries with the next one.
LIST_TIMEOUTS_S: tuple[float, ...] = (10.0, 30.0, 60.0)
#: How often a pending load checks that LM Studio still answers.
LOAD_POLL_S = 5.0
#: How long one liveness check during a load may take.
LOAD_PROBE_TIMEOUT_S = 10.0
#: A load fails once LM Studio has not answered a liveness check for this long.
LOAD_UNRESPONSIVE_S = 180.0
#: The longest a load is waited for while LM Studio keeps answering.
LOAD_CEILING_S = 1800.0
#: The connect bound of the load request itself (the read waits on the polls above).
LOAD_CONNECT_TIMEOUT_S = 10.0

REASON_LIST_UNANSWERED = "lm_studio_list_unanswered"
REASON_LIST_UNREACHABLE = "lm_studio_unreachable"
REASON_LIST_REJECTED = "lm_studio_list_rejected"
REASON_LIST_UNPARSEABLE = "lm_studio_list_unparseable"
REASON_UNRESPONSIVE_DURING_LOAD = "lm_studio_unresponsive_during_load"
REASON_LOAD_CEILING = "lm_studio_load_ceiling"


class LMStudioLoadError(RuntimeError):
    """LM Studio could not be asked, or did not finish loading; ``reason`` is typed."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")


def _list_models(root: str, headers: dict[str, str]) -> Any:
    """``GET {root}/api/v1/models`` with escalating bounds; the response, or a typed error."""
    import requests  # noqa: PLC0415

    for attempt, timeout in enumerate(LIST_TIMEOUTS_S, start=1):
        try:
            return requests.get(f"{root}/api/v1/models", headers=headers, timeout=timeout)
        except requests.Timeout:
            logger.warning(
                "LM Studio model list slow reason=%s root=%s attempt=%d timeout_s=%g",
                "lm_studio_list_slow_retrying"
                if attempt < len(LIST_TIMEOUTS_S)
                else REASON_LIST_UNANSWERED,
                root,
                attempt,
                timeout,
            )
        except requests.ConnectionError as exc:
            raise LMStudioLoadError(
                REASON_LIST_UNREACHABLE, f"LM Studio isn't answering at {root} ({exc})"
            ) from exc
    waited = " / ".join(f"{t:g}s" for t in LIST_TIMEOUTS_S)
    raise LMStudioLoadError(
        REASON_LIST_UNANSWERED,
        f"LM Studio at {root} did not answer its model list (waited {waited}); "
        "it is running but busy or unresponsive",
    )


def loaded_instance_matching(
    root: str,
    headers: dict[str, str],
    *,
    model: str,
    context_length: int,
    parallel: int,
) -> str:
    """The id of a loaded instance of ``model`` with this context and concurrency, else ``""``.

    Raises:
        LMStudioLoadError: LM Studio is unreachable or never answered the listing.
    """
    response = _list_models(root, headers)
    if response.status_code >= 400:
        logger.warning(
            "LM Studio model list refused reason=%s root=%s status=%d; loading the model",
            REASON_LIST_REJECTED,
            root,
            response.status_code,
        )
        return ""
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "LM Studio model list unreadable reason=%s root=%s error=%r; loading the model",
            REASON_LIST_UNPARSEABLE,
            root,
            exc,
        )
        return ""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return ""
    for item in models:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        loaded = item.get("loaded_instances")
        if not isinstance(loaded, list):
            continue
        for instance in loaded:
            if not isinstance(instance, dict):
                continue
            instance_id = str(instance.get("id") or "")
            if model not in {key, instance_id}:
                continue
            config = instance.get("config")
            if not isinstance(config, dict):
                continue
            # Reuse only if BOTH the context and the concurrency cap already match what
            # we'd load -- otherwise a stale parallel=4 instance would be kept and stall.
            if _as_int(config.get("context_length")) == context_length and (
                _as_int(config.get("parallel")) == parallel
            ):
                return instance_id
    return ""


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _answers(root: str, headers: dict[str, str]) -> bool:
    """Whether LM Studio answers its model list right now (any HTTP status)."""
    import requests  # noqa: PLC0415

    try:
        requests.get(f"{root}/api/v1/models", headers=headers, timeout=LOAD_PROBE_TIMEOUT_S)
    except requests.RequestException:
        return False
    return True


def load_model(root: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    """``POST {root}/api/v1/models/load``, waited for while LM Studio stays responsive.

    Returns:
        The load response.

    Raises:
        LMStudioLoadError: LM Studio stopped answering during the load, or kept answering
            past :data:`LOAD_CEILING_S` without finishing it. LM Studio may still finish
            the load on its own; the next bind then finds it loaded.
        requests.RequestException: The load request itself failed (connect refused...).
    """
    import requests  # noqa: PLC0415

    outcome: dict[str, Any] = {}
    done = threading.Event()

    def _post() -> None:
        try:
            outcome["response"] = requests.post(
                f"{root}/api/v1/models/load",
                headers=headers,
                json=body,
                timeout=(LOAD_CONNECT_TIMEOUT_S, None),
            )
        except requests.RequestException as exc:
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_post, daemon=True, name="lm-studio-load").start()
    started = last_answer = time.monotonic()
    model = body.get("model")
    while not done.wait(LOAD_POLL_S):
        now = time.monotonic()
        if _answers(root, headers):
            last_answer = now
        elif now - last_answer >= LOAD_UNRESPONSIVE_S:
            logger.warning(
                "LM Studio load abandoned reason=%s root=%s model=%s silent_s=%.0f",
                REASON_UNRESPONSIVE_DURING_LOAD,
                root,
                model,
                now - last_answer,
            )
            raise LMStudioLoadError(
                REASON_UNRESPONSIVE_DURING_LOAD,
                f"LM Studio at {root} stopped answering for {now - last_answer:.0f}s while "
                f"loading {model!r}",
            )
        if now - started >= LOAD_CEILING_S:
            logger.warning(
                "LM Studio load abandoned reason=%s root=%s model=%s waited_s=%.0f",
                REASON_LOAD_CEILING,
                root,
                model,
                now - started,
            )
            raise LMStudioLoadError(
                REASON_LOAD_CEILING,
                f"LM Studio at {root} is answering but did not finish loading {model!r} "
                f"within {LOAD_CEILING_S:.0f}s",
            )
        logger.info(
            "LM Studio still loading reason=lm_studio_load_pending root=%s model=%s waited_s=%.0f",
            root,
            model,
            now - started,
        )
    if "response" not in outcome:
        raise outcome.get("error") or LMStudioLoadError(
            "lm_studio_load_failed", f"the load request to {root} ended without a response"
        )
    return outcome["response"]


__all__ = [
    "LIST_TIMEOUTS_S",
    "LMStudioLoadError",
    "REASON_LIST_REJECTED",
    "REASON_LIST_UNANSWERED",
    "REASON_LIST_UNPARSEABLE",
    "REASON_LIST_UNREACHABLE",
    "REASON_LOAD_CEILING",
    "REASON_UNRESPONSIVE_DURING_LOAD",
    "load_model",
    "loaded_instance_matching",
]
