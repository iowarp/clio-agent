"""clio_agent.sdk — typed Python client for the GACT v1 API (#799 phase 1).

Talk to a running clio backend from code: sessions, messages, SSE
streaming with ``Last-Event-ID`` resume, workspaces, permissions, and
health/capability probing. Pure client — importing this package never
loads the gact server.

Example:
    >>> from clio_agent.sdk import ClioClient
    >>> with ClioClient() as client:
    ...     print(client.health().overall_status)
"""

from clio_agent.sdk.client import (
    DEFAULT_BASE_URL,
    ClioClient,
    MessagesAPI,
    PermissionsAPI,
    SessionsAPI,
    WorkspacesAPI,
)
from clio_agent.sdk.errors import (
    ClioAPIError,
    ClioConnectionError,
    ClioSDKError,
    ConflictError,
    InternalServerError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    UnsupportedError,
)
from clio_agent.sdk.events import (
    EventStream,
    MessageCompleted,
    MessageCreated,
    MessageDeleted,
    MessagePartAdded,
    MessagePartCompleted,
    MessagePartDelta,
    PermissionRequested,
    PermissionResolved,
    ServerConnected,
    ServerHeartbeat,
    SessionSnapshot,
    SessionStatusChanged,
    SessionUpdated,
    StreamEvent,
    ToolCallCompleted,
    ToolCallStarted,
)
from clio_agent.sdk.types import (
    Capabilities,
    ErrorInfo,
    Health,
    Integration,
    Message,
    Part,
    PermissionList,
    PermissionRequest,
    PostMessageAck,
    Session,
    Tokens,
    Workspace,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "Capabilities",
    "ClioAPIError",
    "ClioClient",
    "ClioConnectionError",
    "ClioSDKError",
    "ConflictError",
    "ErrorInfo",
    "EventStream",
    "Health",
    "Integration",
    "InternalServerError",
    "InvalidRequestError",
    "Message",
    "MessageCompleted",
    "MessageCreated",
    "MessageDeleted",
    "MessagePartAdded",
    "MessagePartCompleted",
    "MessagePartDelta",
    "MessagesAPI",
    "NotFoundError",
    "Part",
    "PermissionDeniedError",
    "PermissionList",
    "PermissionRequest",
    "PermissionRequested",
    "PermissionResolved",
    "PermissionsAPI",
    "PostMessageAck",
    "ServerConnected",
    "ServerHeartbeat",
    "ServiceUnavailableError",
    "Session",
    "SessionSnapshot",
    "SessionStatusChanged",
    "SessionUpdated",
    "SessionsAPI",
    "StreamEvent",
    "Tokens",
    "ToolCallCompleted",
    "ToolCallStarted",
    "UnsupportedError",
    "Workspace",
    "WorkspacesAPI",
]
