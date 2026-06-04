"""File access policy and validation for CLIO tools."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_MAX_FILE_SIZE_BYTES = 1 << 30


def _default_allowed_roots() -> tuple[Path, ...]:
    """Default roots evaluated at policy-creation time, not module-import.

    Capturing Path.cwd() at module-import time was a footgun: the
    clio_agent package may be imported (via DSPy/litellm side effects)
    long before the agent process settles into its real working dir.
    Result: writes to the agent's actual cwd were rejected as 'outside
    allowed roots'. Defer evaluation so each FileAccessPolicy instance
    sees the current cwd at the moment it's constructed.
    """
    return (Path.cwd(), Path("/tmp"))


class FilePolicyError(ValueError):
    """Structured validation error raised before a tool touches a file."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        field: str,
        path: str | None = None,
        next_action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.path = path
        self.next_action = next_action
        self.details = details or {}

    def to_result(self) -> dict[str, Any]:
        """Return a structured tool result error."""
        return {
            "error": {
                "type": "file_policy",
                "code": self.code,
                "message": self.message,
                "field": self.field,
                "path": self.path,
                "next_action": self.next_action,
                "details": self.details,
            }
        }

    def to_text(self) -> str:
        """Return a text error for legacy string-returning chart tools."""
        return "Error: " + json.dumps(self.to_result(), sort_keys=True)


@dataclass(frozen=True)
class FileAccessPolicy:
    """Simple file policy for tool inputs and chart outputs."""

    allowed_roots: tuple[Path, ...]
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    allow_symlinks: bool = False

    @classmethod
    def from_env(cls) -> "FileAccessPolicy":
        """Build policy from CLIO_* environment variables."""
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]) -> "FileAccessPolicy":
        """Build policy from an environment-like mapping."""
        roots_raw = env.get("CLIO_ALLOWED_ROOTS", "")
        if roots_raw.strip():
            roots = tuple(Path(item).expanduser() for item in roots_raw.split(os.pathsep) if item)
        else:
            roots = _default_allowed_roots()

        max_size_raw = env.get("CLIO_MAX_FILE_SIZE_BYTES", "")
        try:
            max_size = int(max_size_raw) if max_size_raw else DEFAULT_MAX_FILE_SIZE_BYTES
        except ValueError as exc:
            raise FilePolicyError(
                code="invalid_policy",
                message=f"CLIO_MAX_FILE_SIZE_BYTES must be an integer, got {max_size_raw!r}.",
                field="CLIO_MAX_FILE_SIZE_BYTES",
                next_action="Set CLIO_MAX_FILE_SIZE_BYTES to a positive integer.",
            ) from exc
        if max_size <= 0:
            raise FilePolicyError(
                code="invalid_policy",
                message="CLIO_MAX_FILE_SIZE_BYTES must be positive.",
                field="CLIO_MAX_FILE_SIZE_BYTES",
                next_action="Set CLIO_MAX_FILE_SIZE_BYTES to a positive integer.",
            )

        allow_symlinks = env.get("CLIO_ALLOW_SYMLINKS", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        return cls(
            allowed_roots=tuple(_resolve_root(root) for root in roots),
            max_file_size_bytes=max_size,
            allow_symlinks=allow_symlinks,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable policy snapshot."""
        return {
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "max_file_size_bytes": self.max_file_size_bytes,
            "allow_symlinks": self.allow_symlinks,
            "read_mode": "existing regular files under allowed roots",
            "write_mode": "explicit output paths under allowed roots",
        }

    def validate_read(self, filepath: str, *, field: str = "filepath") -> Path:
        """Validate a read-only file path and return its resolved path."""
        raw_path = _coerce_path(filepath, field=field)
        if not self.allow_symlinks and _has_symlink(raw_path):
            raise self._error(
                code="symlink_denied",
                message=f"Symlinks are not allowed by file policy: {raw_path}",
                field=field,
                path=str(raw_path),
                next_action="Use a real file path or set CLIO_ALLOW_SYMLINKS=true.",
            )

        try:
            resolved = raw_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise self._error(
                code="file_not_found",
                message=f"File does not exist: {raw_path}",
                field=field,
                path=str(raw_path),
                next_action="Provide an existing file inside an allowed root.",
            ) from exc

        self._ensure_allowed(resolved, field=field)
        if not resolved.is_file():
            raise self._error(
                code="not_a_file",
                message=f"Path is not a regular file: {resolved}",
                field=field,
                path=str(resolved),
                next_action="Provide a regular file path.",
            )

        size = resolved.stat().st_size
        if size > self.max_file_size_bytes:
            raise self._error(
                code="file_too_large",
                message=(
                    f"File size {size} exceeds policy limit {self.max_file_size_bytes}: {resolved}"
                ),
                field=field,
                path=str(resolved),
                next_action="Raise CLIO_MAX_FILE_SIZE_BYTES or use a smaller file sample.",
                details={"size_bytes": size, "max_file_size_bytes": self.max_file_size_bytes},
            )
        return resolved

    def validate_write(
        self,
        filepath: str,
        *,
        field: str = "output_path",
        create_parent: bool = False,
    ) -> Path:
        """Validate an explicit output path and return its resolved path."""
        raw_path = _coerce_path(filepath, field=field)
        parent = raw_path.parent
        if not self.allow_symlinks and (_has_symlink(parent) or raw_path.is_symlink()):
            raise self._error(
                code="symlink_denied",
                message=f"Symlinks are not allowed by file policy: {raw_path}",
                field=field,
                path=str(raw_path),
                next_action="Use a real output path or set CLIO_ALLOW_SYMLINKS=true.",
            )
        try:
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError as exc:
            if create_parent:
                resolved_parent = parent.resolve(strict=False)
                self._ensure_allowed(resolved_parent, field=field)
                parent.mkdir(parents=True, exist_ok=True)
                resolved_parent = parent.resolve(strict=True)
                return resolved_parent / raw_path.name
            raise self._error(
                code="parent_not_found",
                message=f"Output directory does not exist: {parent}",
                field=field,
                path=str(raw_path),
                next_action="Create the output directory inside an allowed root.",
            ) from exc
        self._ensure_allowed(resolved_parent, field=field)
        return resolved_parent / raw_path.name

    def _ensure_allowed(self, path: Path, *, field: str) -> None:
        if any(_is_relative_to(path, root) for root in self.allowed_roots):
            return
        raise self._error(
            code="outside_allowed_roots",
            message=f"Path is outside allowed roots: {path}",
            field=field,
            path=str(path),
            next_action="Move the file under an allowed root or set CLIO_ALLOWED_ROOTS.",
            details={"allowed_roots": [str(root) for root in self.allowed_roots]},
        )

    def _error(
        self,
        *,
        code: str,
        message: str,
        field: str,
        next_action: str,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> FilePolicyError:
        return FilePolicyError(
            code=code,
            message=message,
            field=field,
            path=path,
            next_action=next_action,
            details=details,
        )


def validate_read_path(filepath: str, *, field: str = "filepath") -> Path:
    """Validate a read path with policy loaded from the environment."""
    return FileAccessPolicy.from_env().validate_read(filepath, field=field)


def validate_write_path(
    filepath: str,
    *,
    field: str = "output_path",
    create_parent: bool = False,
) -> Path:
    """Validate a write path with policy loaded from the environment."""
    return FileAccessPolicy.from_env().validate_write(
        filepath,
        field=field,
        create_parent=create_parent,
    )


def validate_choice(value: str, allowed: set[str], *, field: str) -> None:
    """Validate a string enum before tool execution."""
    if value in allowed:
        return
    raise FilePolicyError(
        code="invalid_argument",
        message=f"Invalid {field}: {value!r}. Expected one of {sorted(allowed)}.",
        field=field,
        next_action=f"Use one of: {', '.join(sorted(allowed))}.",
        details={"allowed": sorted(allowed), "received": value},
    )


def validate_positive_int(value: int, *, field: str, max_value: int | None = None) -> None:
    """Validate positive integer tool arguments."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FilePolicyError(
            code="invalid_argument",
            message=f"{field} must be a positive integer.",
            field=field,
            next_action=f"Set {field} to a positive integer.",
            details={"received": value},
        )
    if max_value is not None and value > max_value:
        raise FilePolicyError(
            code="invalid_argument",
            message=f"{field} must be <= {max_value}.",
            field=field,
            next_action=f"Set {field} to {max_value} or less.",
            details={"received": value, "max": max_value},
        )


def validate_non_empty_string(value: str, *, field: str) -> None:
    """Validate required non-empty string arguments."""
    if isinstance(value, str) and value.strip():
        return
    raise FilePolicyError(
        code="invalid_argument",
        message=f"{field} must be a non-empty string.",
        field=field,
        next_action=f"Provide a non-empty {field}.",
        details={"received": value},
    )


def _coerce_path(filepath: str, *, field: str) -> Path:
    if not isinstance(filepath, str) or not filepath.strip():
        raise FilePolicyError(
            code="invalid_argument",
            message=f"{field} must be a non-empty string path.",
            field=field,
            next_action=f"Provide a non-empty {field}.",
            details={"received": filepath},
        )
    path = Path(filepath).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _resolve_root(root: Path) -> Path:
    try:
        return root.resolve(strict=False)
    except RuntimeError:
        return root.absolute()


def _has_symlink(path: Path) -> bool:
    path = path if path.is_absolute() else Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            return True
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
