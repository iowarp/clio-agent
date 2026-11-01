"""The plugin tools page: each tool's permission and I/O, and saving the person's choices.

Reads (``GET /v1/settings/tools``), for one installed plugin:

- its tools, grouped into read-only and write tools by their flags: MCP
  ``readOnlyHint`` / ``destructiveHint`` annotations (from the agent runtime's loaded
  MCP definitions), CLIO's built-in annotations (the tool catalog), a built-in's
  declared ``read_only`` (recorded when an agent was built) and a Python tool's
  ``read_only``; a tool whose flags are not known yet is in the write group, as the
  gate treats it;
- for each tool the plugin's declared defaults, the deployment's ``tools.permissions``
  and ``tools.callers``, the person's saved rules that name it (with their scope), and
  what happens in each approval mode for a direct call and for a call from a
  ``run_code`` script.

Writes (``PUT /v1/settings/tools``): the person's permission is a policy rule in the
existing store (``/v1/policies``, validated by ``_validate_permission_policies``), one
rule per tool for every workspace; their I/O choice is ``tools.callers`` in the user
``config.yaml`` (:mod:`clio_agent.user_config_document`), since no policy concept
exists for it. No new store.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, get_args

from clio_agent import conf
from clio_agent.gact.code_mode.grants import COVERING_POSTURES
from clio_agent.gact.runtime import tool_permission_layers as layers
from clio_agent.tools.tool_settings import (
    CALLERS,
    CALLERS_BOTH,
    CALLERS_DIRECT,
    PERMISSIONS,
    annotation_permission,
    configured_callers,
    configured_permissions,
)
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)

__all__ = ["approval_modes", "remove_rule", "save_tool_settings", "tools_view"]

#: Outcome words the page shows (with the mode's name beside them).
RUNS = "runs"
ASKS = "asks"
BLOCKED = "blocked"
REVIEWED = "reviewed"
COVERED = "covered"
UNAVAILABLE = "unavailable"


def approval_modes() -> tuple[str, ...]:
    """The session approval modes, from the session defaults' own type."""
    from clio_agent.gact.session_defaults import SessionDefaults  # noqa: PLC0415

    return tuple(get_args(SessionDefaults.model_fields["approval_mode"].annotation))


def _plugin(app: Any, ref: str) -> Any:
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    service = plugin_service(app)
    service.ready()
    for record in service.store.records():
        if f"{record.marketplace}/{record.id}" == ref:
            return record
    raise StoreError("plugin_not_found", f"Plugin {ref!r} is not installed")


def _mcp_definitions(app: Any) -> dict[str, Any]:
    executor = getattr(getattr(app.state, "agent", None), "tool_executor", None)
    get_definitions = getattr(executor, "get_all_tool_definitions", None)
    definitions = get_definitions() if callable(get_definitions) else {}
    return dict(definitions) if isinstance(definitions, Mapping) else {}


def _flags(annotations: Mapping[str, Any] | None) -> tuple[bool, bool]:
    from clio_agent.tools.catalog import annotations_are_read_only  # noqa: PLC0415

    destructive = bool(annotations and annotations.get("destructiveHint") is True)
    return annotations_are_read_only(annotations), destructive


def _tool_rows(app: Any, record: Any) -> list[dict[str, Any]]:
    """``name``, ``kind``, ``server``, flags and description for the plugin's tools."""
    from clio_agent.gact.routes.catalog_runtime_tools import runtime_tool_value  # noqa: PLC0415
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415
    from clio_agent.tools.catalog import (  # noqa: PLC0415
        _BUILTIN_ANNOTATIONS,
        normalize_mcp_annotations,
    )

    facts = per_app_dict("tool_facts", app=app)
    rows: list[dict[str, Any]] = []
    for tool in record.tools:
        row = {"name": tool.name, "kind": tool.kind, "server": "", "description": tool.description}
        if tool.name in _BUILTIN_ANNOTATIONS:
            read_only, destructive = _flags(_BUILTIN_ANNOTATIONS[tool.name])
            row.update(read_only=read_only, destructive=destructive, flags_known=True)
        elif tool.kind == "python":
            row.update(read_only=tool.read_only, destructive=False, flags_known=True)
        elif tool.name in facts:
            fact = facts[tool.name]
            row.update(
                read_only=fact["read_only"],
                destructive=False,
                flags_known=True,
                self_gated=fact["self_gated"],
                own_callers=fact["callers"],
                description=fact["description"],
            )
        else:
            row.update(read_only=False, destructive=False, flags_known=False)
        rows.append(row)
    definitions = _mcp_definitions(app)
    for server in sorted(record.mcp_servers):
        found = False
        for key, definition in sorted(definitions.items()):
            name = str(runtime_tool_value(definition, "name") or key)
            if not name.startswith(f"{server}_"):
                continue
            found = True
            read_only, destructive = _flags(normalize_mcp_annotations(definition))
            rows.append(
                {
                    "name": name,
                    "kind": "mcp",
                    "server": server,
                    "description": str(runtime_tool_value(definition, "description") or ""),
                    "read_only": read_only,
                    "destructive": destructive,
                    "flags_known": True,
                }
            )
        if not found:
            rows.append({"name": f"{server}_*", "kind": "mcp_pending", "server": server})
    return rows


def _person_rules(app: Any, name: str) -> list[dict[str, Any]]:
    """The person's saved rules that name ``name`` exactly, with their scope in words."""
    rules = []
    for policy in getattr(app.state, "permission_policies", []) or []:
        if not isinstance(policy, Mapping) or policy.get("tool_name_pattern") != name:
            continue
        scope = str(policy.get("scope") or "")
        scope_id = str(policy.get("scope_id") or "")
        rules.append(
            {
                "scope": scope,
                "scope_id": scope_id,
                "where": "everywhere" if not scope_id else scope,
                "action": str(policy.get("action") or ""),
                "path_pattern": str(policy.get("path_pattern") or ""),
            }
        )
    return rules


def _everywhere_rule(rules: Iterable[Mapping[str, Any]]) -> str:
    for rule in rules:
        if rule["where"] == "everywhere" and not rule["path_pattern"]:
            return (
                "ask"
                if rule["action"] == "ask"
                else "deny"
                if rule["action"] == "deny"
                else "allow"
            )
    return ""


def _outcome(row: Mapping[str, Any], rule: str, default: str, mode: str, script: bool) -> str:
    """What happens to a call of the tool in ``mode`` (direct, or from a script)."""
    layered = layers.decide(rule, default, approval_mode=mode)
    if layered.action == "deny":
        return BLOCKED
    if script and row["callers"] == CALLERS_DIRECT:
        return UNAVAILABLE
    if not script and row["callers"] == "code":
        return UNAVAILABLE  # only in scripts (offered directly when an agent lacks run_code)
    if layered.action:
        return {"deny": BLOCKED, "allow": RUNS, "ask": ASKS}[layered.action]
    if row.get("read_only"):
        return RUNS
    if row["kind"] in ("native", "python") and not row.get("self_gated"):
        return RUNS  # CLIO's own and plugins' Python tools act without the mode's question
    from clio_agent.gact.permission_gate import default_decision  # noqa: PLC0415

    if default_decision(mode, "tool", row["name"], {}) == "allow":
        return RUNS
    if script and mode in COVERING_POSTURES:
        return COVERED
    return REVIEWED if mode == "ai-review" else ASKS


def tools_view(app: Any, ref: str) -> dict[str, Any]:
    """The tools page for the installed plugin ``ref`` (``marketplace/plugin``)."""
    record = _plugin(app, ref)
    disclosure = record.disclosure.get("servers") or {}
    deployment_permissions = configured_permissions()
    deployment_callers = configured_callers()
    tools = []
    pending = []
    for row in _tool_rows(app, record):
        if row["kind"] == "mcp_pending":
            pending.append(row["server"])
            continue
        name = row["name"]
        declared_row = next((t for t in record.tools if t.name == name), None)
        server = disclosure.get(row["server"]) or {}
        bare = name[len(row["server"]) + 1 :] if row["server"] else ""
        entry = (server.get("tools") or {}).get(bare) or {}
        plugin_permission = (
            (declared_row.permission if declared_row else None)
            or entry.get("permission")
            or server.get("permission")
        )
        plugin_callers = (
            (declared_row.callers if declared_row else None)
            or entry.get("callers")
            or server.get("callers")
            or row.get("own_callers")
            or CALLERS_BOTH
        )
        rules = _person_rules(app, name)
        default = deployment_permissions.get(name) or plugin_permission or ""
        callers = deployment_callers.get(name) or plugin_callers
        row.update(
            group="read" if row["read_only"] else "write",
            plugin_permission=plugin_permission
            or annotation_permission(row["read_only"], row["destructive"]),
            plugin_permission_source="plugin" if plugin_permission else "annotations",
            configured_permission=deployment_permissions.get(name, ""),
            rules=rules,
            permission=_everywhere_rule(rules),
            plugin_callers=plugin_callers,
            # A built-in's own declaration is read from the tool as built: before an agent
            # has loaded it, ``both`` here may not be its real default.
            plugin_callers_known=bool(
                row.get("flags_known") or row["kind"] != "native" or plugin_callers != CALLERS_BOTH
            ),
            configured_callers=deployment_callers.get(name, ""),
            callers=callers,
        )
        rule = _everywhere_rule(rules)
        row["outcomes"] = {
            mode: {
                "direct": _outcome(row, rule, default, mode, False),
                "script": _outcome(row, rule, default, mode, True),
            }
            for mode in approval_modes()
        }
        tools.append(row)
    return {
        "plugin": ref,
        "approval_modes": list(approval_modes()),
        "covering_modes": sorted(COVERING_POSTURES & set(approval_modes())),
        "tools": tools,
        "servers_not_loaded": pending,
    }


def save_tool_settings(app: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """Save the person's permission and/or I/O choice for ``body["tools"]``.

    ``permission``: ``allow``, ``ask``, ``deny`` or ``null`` (remove the person's rule
    for every workspace). ``callers``: ``direct``, ``code``, ``both`` or ``null`` (back to
    the plugin's default). Raises ``ValueError`` on a bad request.
    """
    names = body.get("tools")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise ValueError("tools must be a non-empty list of tool names")
    if "permission" in body:
        value = body["permission"]
        if value is not None and value not in PERMISSIONS:
            raise ValueError(f"permission must be one of {', '.join(PERMISSIONS)} or null")
        _save_rules(app, names, value)
    if "callers" in body:
        value = body["callers"]
        if value is not None and value not in CALLERS:
            raise ValueError(f"callers must be one of {', '.join(CALLERS)} or null")
        _save_callers(names, value)
    if not {"permission", "callers"} & set(body):
        raise ValueError("send permission and/or callers")
    return {"tools": names}


def _everywhere(policy: Mapping[str, Any], name: str) -> bool:
    return (
        policy.get("tool_name_pattern") == name
        and str(policy.get("scope") or "") == "workspace"
        and not policy.get("scope_id")
        and not policy.get("path_pattern")
    )


def _store(app: Any, policies: list[Any]) -> None:
    from clio_agent.gact.runtime.grant_resolver import migrate_priorities  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        _flush_permission_policies,
        _validate_permission_policies,
    )

    clean, errors = _validate_permission_policies(policies)
    if errors:
        raise ValueError(f"invalid permission policies: {errors}")
    app.state.permission_policies = migrate_priorities(clean)
    _flush_permission_policies(app)


def _save_rules(app: Any, names: list[str], action: str | None) -> None:
    from clio_agent.gact.runtime.grant_resolver import migrate_priorities  # noqa: PLC0415

    policies = migrate_priorities(list(getattr(app.state, "permission_policies", []) or []))
    policies = [p for p in policies if not any(_everywhere(p, n) for n in names)]
    if action is not None:
        # Above every rule already saved for the tool: the page's choice is the latest one.
        top = max((int(p.get("priority") or 0) for p in policies), default=0)
        for name in names:
            policies.append(
                {
                    "scope": "workspace",
                    "scope_id": "",
                    "kind": "tool",
                    "tool_name_pattern": name,
                    "action": action,
                    "priority": top + 1,
                    "source": "tools_page",
                }
            )
    _store(app, policies)


def remove_rule(app: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the person's saved rule(s) for one tool matching scope, id, action and path."""
    name = body.get("tool")
    if not isinstance(name, str) or not name:
        raise ValueError("tool is required")
    keys = ("scope", "scope_id", "action", "path_pattern")
    want = {key: str(body.get(key) or "") for key in keys}
    policies = list(getattr(app.state, "permission_policies", []) or [])
    kept = [
        p
        for p in policies
        if not (
            isinstance(p, Mapping)
            and p.get("tool_name_pattern") == name
            and all(str(p.get(key) or "") == want[key] for key in keys)
        )
    ]
    if len(kept) == len(policies):
        raise LookupError("No saved rule matches")
    _store(app, kept)
    return {"removed": len(policies) - len(kept)}


def _save_callers(names: list[str], value: str | None) -> None:
    with USER_CONFIG_LOCK:
        path = user_config_path()
        document = read_document(path)
        tools = dict(document.get("tools") or {})
        callers = dict(tools.get("callers") or {})
        for name in names:
            if value is None:
                callers.pop(name, None)
            else:
                callers[name] = value
        tools["callers"] = callers
        document["tools"] = tools
        write_document(path, document)
        conf.reload()
