"""Remove secrets from text that leaves this computer in a problem report.

Everything a problem report carries (log lines, health summaries, error messages,
and conversation text when the person includes it) passes through one
:class:`Redactor` on the server, so no client can skip it. The rules, in order:

1. **Known values.** The exact secrets this CLIO holds: the GACT bearer token, saved
   provider API keys, connection sign-in tokens and connector secret values (the
   credential stores under the user config folder), sensitive keys in
   ``config.yaml`` and the values of secret-named environment variables.
2. **Private key blocks** (``-----BEGIN ... PRIVATE KEY-----``).
3. **Credential headers**: ``Authorization``, ``Proxy-Authorization``, ``Cookie``,
   ``Set-Cookie`` and API-key headers, whether logged as ``Name: value`` or as a
   JSON / Python mapping entry.
4. **Bearer / Basic credentials** anywhere.
5. **Credentials in URLs**: the user-info part (``https://user:pass@host``) and
   sensitive query parameters, including OAuth ``code``.
6. **Sensitive assignments**: ``api_key=...``, ``"password": "..."``,
   ``client_secret: ...`` and the like.
7. **Token shapes**: well-known key formats (``sk-``, ``ghp_``, ``github_pat_``,
   ``xox?-``, ``AKIA``, ``AIza``, ``hf_``, ``glpat-``, ``ya29.``, JWTs).
8. **Home folder**: the home directory path becomes ``~``.

Each match becomes ``[redacted]`` (the house convention, see
:data:`clio_agent.gact.semantic_events.REDACTED_VALUE`). Counts per rule are kept so
the report's manifest can say what was removed without saying what it was.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

REDACTED = "[redacted]"

#: Key names whose value is a credential (compared case-insensitively, ``-`` as ``_``).
SENSITIVE_NAMES = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "bearer_token",
    "session_token",
    "client_secret",
    "private_key",
    "secret_key",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "token",
    "authorization",
    "cookie",
    "x_api_key",
    "credential",
    "credentials",
)

_NAME_ALT = "|".join(
    sorted({re.escape(name).replace("_", "[_-]?") for name in SENSITIVE_NAMES}, key=len)[::-1]
)
#: Environment variable name fragments that mark a secret.
_ENV_SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH)", re.I)
#: Environment names that carry a location or a switch, never a secret.
_ENV_NOT_SECRET = re.compile(r"(_DIR|_PATH|_FILE|_HOME|_MODE|_ENABLED|_URL_ENV|_PROVIDER)$", re.I)
_MIN_SECRET_CHARS = 8

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.S,
)
# ``Authorization: Bearer x`` as a header line, or the same pair in a JSON / repr mapping.
_HEADER_NAMES = (
    r"authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|"
    r"x-goog-api-key|x-auth-token|anthropic-api-key|openai-api-key"
)
_HEADER_LINE = re.compile(rf"(?im)\b((?:{_HEADER_NAMES}))(\s*:\s*)(?![\"'])([^\r\n]+)")
_HEADER_MAPPING = re.compile(
    rf"(?i)([\"']({_HEADER_NAMES})[\"']\s*[:,]\s*[\"'])((?:\\.|[^\"'\\])*)([\"'])"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+(?!\[redacted\])([A-Za-z0-9._~+/=-]{8,})")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@:\"'<>]+(?::[^/\s@\"'<>]*)?)@")
_URL_PARAM = re.compile(
    r"(?i)([?&#;](?:code|state|access_token|refresh_token|id_token|auth_token|token|"
    r"api_key|apikey|key|sig|signature|client_secret|password|secret)=)([^&#\s\"'<>]+)"
)
_ASSIGNED = re.compile(
    rf"(?i)((?<![A-Za-z0-9])[\w.-]*?(?:{_NAME_ALT}))([\"']?\s*(?:=|:|=>)\s*[\"']?)"
    r"(?!(?:bearer|basic)\b)(?!\[redacted\])(?!(?:null|none|true|false|\d+)\b[\"',;}\s)]?)"
    r"([^\s\"',;}\)\]]{4,})"
)
_TOKEN_SHAPES = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|npm_[A-Za-z0-9]{30,}"
    r"|ya29\.[A-Za-z0-9._-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)

RULES = (
    ("known_value", "A secret this CLIO holds (saved keys, sign-in tokens, connector secrets)"),
    ("private_key", "Private key blocks"),
    ("credential_header", "Authorization, cookie and API-key headers"),
    ("bearer", "Bearer and Basic credentials"),
    ("url_credentials", "User names, passwords and tokens in web addresses"),
    ("assignment", "Values of password, token, secret and API-key settings"),
    ("token_shape", "Text shaped like a known API key or access token"),
    ("home_path", "Your home folder path, shown as ~"),
)


def _normal_name(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def is_sensitive_name(key: str) -> bool:
    """Whether a mapping key or setting name holds a credential."""

    name = _normal_name(key)
    return any(name == item or name.endswith(f"_{item}") for item in SENSITIVE_NAMES) or (
        name.endswith(("token", "secret", "password", "apikey", "api_key"))
    )


def _walk_secret_values(value: Any, *, inside: bool = False) -> Iterator[str]:
    """String leaves stored under a sensitive key, or anywhere inside a ``secrets`` map."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_s = str(key)
            yield from _walk_secret_values(
                item, inside=inside or is_sensitive_name(key_s) or _normal_name(key_s) == "secrets"
            )
    elif isinstance(value, list):
        for item in value:
            yield from _walk_secret_values(item, inside=inside)
    elif inside and isinstance(value, str):
        yield value


def environment_secrets(env: Mapping[str, str] | None = None) -> set[str]:
    """Values of environment variables whose name marks a secret."""

    source = os.environ if env is None else env
    found: set[str] = set()
    for name, value in source.items():
        if not _ENV_SECRET.search(name) or _ENV_NOT_SECRET.search(name):
            continue
        text = value.strip()
        if len(text) < _MIN_SECRET_CHARS or text.lower() in {"true", "false"}:
            continue
        if text.isdigit() or text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:\\", text):
            continue
        found.add(text)
    return found


def stored_secrets(config_dir: Path) -> set[str]:
    """Secrets saved in CLIO's credential stores and ``config.yaml`` under ``config_dir``."""

    found: set[str] = set()
    for name in ("provider_api_keys.json", "mcp_oauth_tokens.json", "codex_credentials.json"):
        path = config_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # Absent or unreadable: nothing of it can reach the report either.
        found.update(_walk_secret_values(data))
    config = config_dir / "config.yaml"
    try:
        loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        loaded = None  # Absent or malformed: its values cannot be read into a log either.
    if loaded is not None:
        found.update(_walk_secret_values(loaded))
    return {value for value in found if len(value.strip()) >= _MIN_SECRET_CHARS}


class Redactor:
    """Applies :data:`RULES` to text and counts what each rule removed."""

    def __init__(self, known: Iterable[str] = (), home: str | None = None) -> None:
        values = {value for value in known if value and len(value) >= _MIN_SECRET_CHARS}
        # Longest first, so a secret that contains another is removed whole.
        self._known = sorted(values, key=len, reverse=True)
        self._homes = _home_spellings(home)
        self.counts: Counter[str] = Counter()

    def _sub(self, rule: str, pattern: re.Pattern[str], repl: Any, text: str) -> str:
        result, count = pattern.subn(repl, text)
        if count:
            self.counts[rule] += count
        return result

    def apply(self, text: str) -> str:
        """``text`` with every rule applied."""

        if not text:
            return text
        for value in self._known:
            if value in text:
                self.counts["known_value"] += text.count(value)
                text = text.replace(value, REDACTED)
        text = self._sub("private_key", _PRIVATE_KEY, REDACTED, text)
        text = self._sub(
            "credential_header",
            _HEADER_MAPPING,
            lambda m: f"{m.group(1)}{REDACTED}{m.group(4)}",
            text,
        )
        text = self._sub(
            "credential_header",
            _HEADER_LINE,
            lambda m: (
                m.group(0)
                if m.group(3).startswith(REDACTED)
                else f"{m.group(1)}{m.group(2)}{REDACTED}"
            ),
            text,
        )
        text = self._sub("bearer", _BEARER, lambda m: f"{m.group(1)} {REDACTED}", text)
        text = self._sub(
            "url_credentials", _URL_USERINFO, lambda m: f"{m.group(1)}{REDACTED}@", text
        )
        text = self._sub("url_credentials", _URL_PARAM, lambda m: f"{m.group(1)}{REDACTED}", text)
        text = self._sub(
            "assignment", _ASSIGNED, lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text
        )
        text = self._sub("token_shape", _TOKEN_SHAPES, REDACTED, text)
        for _home, pattern in self._homes:
            text = self._sub("home_path", pattern, "~", text)
        return text

    def summary(self) -> list[dict[str, Any]]:
        """Every rule with how many matches it removed, for the report's manifest."""

        return [
            {"rule": rule, "description": description, "count": self.counts.get(rule, 0)}
            for rule, description in RULES
        ]


def _home_spellings(home: str | None) -> list[tuple[str, re.Pattern[str]]]:
    """Patterns for the home folder: as given, resolved, and with either slash."""

    if home is None:
        try:
            home = str(Path.home())
        except RuntimeError:
            return []
    spellings: set[str] = set()
    for raw in (home, os.path.realpath(home)):
        stripped = raw.rstrip("/\\")
        if len(stripped) < 2:  # "/" as home would replace every path separator
            continue
        spellings.update({stripped, stripped.replace("\\", "/"), stripped.replace("/", "\\")})
    return [
        # A path boundary must follow, so /home/al does not eat /home/alice.
        (spelling, re.compile(re.escape(spelling) + r"(?=[/\\\s\"':,;)\]}]|$)", re.M))
        for spelling in sorted(spellings, key=len, reverse=True)
    ]


def redactor_for_this_computer(config_dir: Path | None, extra: Iterable[str] = ()) -> Redactor:
    """A :class:`Redactor` that knows this CLIO's stored and configured secrets."""

    known = set(extra) | environment_secrets()
    if config_dir is not None:
        known |= stored_secrets(config_dir)
    return Redactor(known)


__all__ = [
    "REDACTED",
    "RULES",
    "Redactor",
    "environment_secrets",
    "is_sensitive_name",
    "redactor_for_this_computer",
    "stored_secrets",
]
