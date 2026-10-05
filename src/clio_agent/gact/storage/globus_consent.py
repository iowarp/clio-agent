"""Collection-scoped Globus consent requests, without credentials or arbitrary scopes."""

from globus_sdk.scopes import Scope

from clio_agent.gact.storage.models import SourceRecord

TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"


class GlobusConsentRequired(PermissionError):
    """The approved source requires a further browser authorization step."""

    def __init__(self, scopes: list[str]) -> None:
        super().__init__("Authorize this Globus collection to browse or transfer its files.")
        self.scopes = scopes


def collection_scopes(record: SourceRecord, requested: list[str]) -> list[str]:
    """Accept Transfer dependencies and HTTPS scopes for the approved source only."""
    if record.source.provider != "globus" or not requested or len(requested) > 8:
        raise ValueError("Invalid Globus collection consent request")
    allowed = {
        f"https://auth.globus.org/scopes/{identifier}/data_access"
        for identifier in (
            record.configuration.collection_id,
            record.configuration.destination_collection_id,
        )
        if identifier
    }
    result: set[str] = set()
    direct = {
        f"https://auth.globus.org/scopes/{record.configuration.collection_id}/{suffix}"
        for suffix in ("https", "data_access")
    }
    for value in requested:
        try:
            if len(value) > 4096:
                raise ValueError("Scope too long")
            scope = Scope.parse(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Invalid Globus collection consent request") from exc
        if scope.scope_string in direct and not scope.dependencies:
            result.add(str(scope))
            continue
        if (
            scope.scope_string != TRANSFER_SCOPE
            or not scope.dependencies
            or any(
                child.scope_string not in allowed or child.dependencies
                for child in scope.dependencies
            )
        ):
            raise ValueError("Globus requested consent outside this source's collections")
        result.add(str(scope))
    return sorted(result)
