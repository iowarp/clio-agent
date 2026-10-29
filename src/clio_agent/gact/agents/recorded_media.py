"""Hydrate bounded recorded tool media into provider-native result parts."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any

from dspy.lm15 import (
    DocumentPart,
    ImagePart,
    TextPart,
    ToolResultPart,
)

from clio_agent.gact.agents.media_cache import cached_media

logger = logging.getLogger(__name__)


def result_part(
    call_id: str, name: str, value: Any, is_error: bool, *, call_ref: str = ""
) -> ToolResultPart:
    """A tool's result as the provider-native part (images/PDFs as media, else text).

    Media the history can no longer show (its snapshot is gone) becomes a note the
    agent reads -- one old image never fails the whole conversation. ``call_ref``
    (``call:<session>/<call_id>``) follows the result, once, so the call can be cited.
    """
    part = _result_part(call_id, name, value, is_error)
    if not call_ref:
        return part
    return ToolResultPart(
        id=part.id,
        content=(*part.content, TextPart(text=f"[{call_ref}]")),
        name=part.name,
        is_error=part.is_error,
    )


def _result_part(call_id: str, name: str, value: Any, is_error: bool) -> ToolResultPart:
    try:
        media = _media(value)
    except ValueError as exc:  # ViewImageError / ViewPdfError / ViewedMediaUnavailable
        logger.warning(
            "tool media unavailable reason=%s call=%s detail=%s", type(exc).__name__, call_id, exc
        )
        note = (
            f"[clio: media_unavailable] The media this call returned can no longer be shown: {exc}"
        )
        return ToolResultPart(
            id=call_id, content=(TextPart(text=note),), name=name, is_error=is_error
        )
    content: tuple[Any, ...] = (
        (media,) if media is not None else (TextPart(text=_observation_text(value)),)
    )
    if (
        media is not None
        and isinstance(value, Mapping)
        and (value.get("capture") or isinstance(media, DocumentPart))
    ):
        # Provider media remain ephemeral; their compact, exact provenance
        # must also reach the model instead of being lost during hydration.
        content += (
            TextPart(
                text=_observation_text(
                    {
                        key: item
                        for key, item in value.items()
                        if key not in {"snapshot", "snapshot_sha256"}
                    }
                )
            ),
        )
    return ToolResultPart(id=call_id, content=content, name=name, is_error=is_error)


def _media(value: Any) -> ImagePart | DocumentPart | None:
    """Hydrate a view_image / view_pdf result descriptor into a native media part."""
    from clio_agent.gact.view_image_tool import (
        _hydrate_descriptor as image_hydrate,  # noqa: PLC0415
    )
    from clio_agent.gact.view_image_tool import _is_descriptor as is_image  # noqa: PLC0415
    from clio_agent.gact.view_pdf_tool import _hydrate_descriptor as pdf_hydrate  # noqa: PLC0415
    from clio_agent.gact.view_pdf_tool import _is_descriptor as is_pdf  # noqa: PLC0415

    if not isinstance(value, Mapping):
        return None
    if is_image(value):

        def image() -> ImagePart:
            media, _size = image_hydrate(value)
            media_type, data = _split_data_url(str(getattr(media, "url", "")))
            return ImagePart(data=data, media_type=media_type)

        return cached_media("image", value, image, lambda part: len(part.data or ""))
    if is_pdf(value):

        def document() -> DocumentPart:
            media, _size = pdf_hydrate(value)
            media_type, data = _split_data_url(
                str(getattr(media, "url", "") or getattr(media, "file_data", ""))
            )
            return DocumentPart(data=data, media_type=media_type or "application/pdf")

        return cached_media("document", value, document, lambda part: len(part.data or ""))
    return None


def _split_data_url(url: str) -> tuple[str, str]:
    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").split(";", 1)[0]
    if not data:
        raise ValueError("an attachment descriptor did not hydrate to inline data")
    base64.b64decode(data, validate=True)  # typed failure on a corrupt payload
    return media_type, data


def _observation_text(value: Any) -> str:
    if isinstance(value, str):
        return value or "(empty result)"
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
