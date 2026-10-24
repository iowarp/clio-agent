"""Actual model discovery and exact peer-session messaging at busy and idle boundaries."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Any

import _common as common
import _lifecycle_evidence as evidence


def peer_cycle(call: Any, proof: Path, wid: str, agent: str) -> dict[str, Any]:
    """Exercise lexical discovery, scoped addressing, iteration delivery and idle wake."""
    output = proof / "peer-messaging"
    output.mkdir()
    workspace = proof / "workspace"
    # Test exact peer routing with autonomous roots. Coordinator delegation is a
    # separate behavior; it must not substitute a child lacking memory tools, so
    # the peer is its own agent without the delegation plugin, keeping the
    # coordinator prompt's run-specific instructions.
    coordinator = common.testing_prompt(workspace / "testing-marketplaces/task-subagent-testing")
    body = (
        coordinator.read_text(encoding="utf8")
        .split("---", 2)[2]
        .replace(
            "Spawn only the explicitly requested worker assignment. Use shared task controls\n"
            "yourself for status and waits; do not delegate those observations to the worker.",
            "Execute the peer discovery, messaging and task-control mission yourself.\n"
            "Do not spawn or delegate any of those actions.",
        )
        + "\nPerform this peer messaging mission directly with your own declared tools.\n"
    )
    peer_pack = common.materialize_testing_marketplace(
        Path(__file__).resolve().parent / "agents" / "task-testing",
        workspace,
        {},
        name="peer-testing",
        agent_id="lifecycle-peer-testing",
    )
    prompt = common.testing_prompt(peer_pack)
    header = prompt.read_text(encoding="utf8").split("---", 2)[1]
    prompt.write_text("---" + header + "---\n" + body.lstrip("\n"), encoding="utf8")
    agent = common.install_testing_agent(call, peer_pack)
    marker = "PEER_DISCOVERY_" + secrets.token_hex(8)
    busy_message = "BUSY_PEER_MESSAGE_" + secrets.token_hex(8)
    idle_message = "IDLE_PEER_MESSAGE_" + secrets.token_hex(8)
    paths = evidence.worker_files(workspace, "peer-busy", "peer-work-complete")
    peer = common.create_session(call, wid, marker)
    sender = common.create_session(call, wid, "authorized peer sender")
    for sid in (peer, sender):
        common.use_agent(call, sid, agent)
    command = common.quoted_command(sys.executable, str(paths["script"]))
    common.post_message(
        call,
        peer,
        f"This session's discovery marker is {marker}. Start exactly one "
        f"shell_bash(command={command!r},background=true,timeout_s=0,cwd={str(workspace)!r}). "
        "Then wait_tasks on its handle without a timeout. After the wait, read "
        f"fs_read_file(filepath={str(workspace / 'sentinel.txt')!r}), report any agent messages, and finish. "
        "The human authorizes a single reply to the exact source session of each qualification "
        "message, scope='workspace', user_intent='human requested peer messaging qualification', "
        "reply_to=its message_id, retaining correlation_id. Say ACK plus its text. Never reply to an ACK. "
        "If another message wakes you while idle, follow the same rule. No other task submission.",
    )
    evidence.require(
        lambda: evidence.tool_events(proof, peer, "wait_tasks", complete=False),
        "peer model busy in its committed task wait",
    )
    discovery = evidence.model_turn(
        call,
        proof,
        sender,
        f"The human authorizes sending one qualification message to the peer in this workspace "
        f"whose transcript contains {marker!r}. Find it using session_search(query={marker!r}, "
        "scope='workspace',user_intent='human requested peer messaging qualification'). "
        "Read that exact session with session_read_summary using the same scope/intent. "
        f"Then message_agent(target=the returned exact session address,message={busy_message!r}, "
        "scope='workspace',user_intent='human requested peer messaging qualification', "
        "correlation_id='lifecycle-peer-busy'). Use these native tools yourself; do not spawn "
        "a worker or delegate any step. Finish without waiting or replying. "
        "Report any acknowledgement that later wakes you; never reply to an ACK.",
        "message_agent",
    )
    ack = evidence.observation(discovery["invocation"])
    assert ack["target_session_id"] == peer and ack["source_session_id"] == sender
    assert evidence.tool_events(proof, sender, "session_search")
    assert evidence.tool_events(proof, sender, "session_read_summary")
    held = next(row for row in evidence.mail(call, peer) if row["message_id"] == ack["message_id"])
    assert not held["delivered_at"], "Busy waiter mail must wait for its next model iteration"
    paths["release"].write_text("release real peer work", encoding="utf8")
    reply = evidence.require(
        lambda: next(
            (
                event
                for event in evidence.tool_events(proof, peer, "message_agent")
                if evidence.observation(event).get("source_session_id") == peer
            ),
            None,
        ),
        "peer model replied to actual busy-boundary message",
    )
    evidence.require(
        lambda: evidence.settled_turn(call, peer), "peer finished after its next iteration"
    )
    read_calls = evidence.tool_events(proof, peer, "fs_read_file", complete=False)
    assert len(read_calls) == 1
    delivered = next(
        row for row in evidence.mail(call, peer) if row["message_id"] == ack["message_id"]
    )
    assert delivered["delivered_at"]
    reply_ack = evidence.observation(reply)
    reply_mail = next(
        row for row in evidence.mail(call, sender) if row["message_id"] == reply_ack["message_id"]
    )
    assert reply_mail["reply_to"] == ack["message_id"]
    evidence.require(
        lambda: next(
            (
                row
                for row in evidence.mail(call, sender)
                if row["message_id"] == reply_ack["message_id"] and row["delivered_at"]
            ),
            None,
        ),
        "idle sender automatically woke for the peer reply",
    )
    evidence.require(
        lambda: evidence.settled_turn(call, sender), "sender acknowledgement wake settled"
    )
    idle = evidence.model_turn(
        call,
        proof,
        sender,
        f"Human-authorized second peer message: call message_agent(target={peer!r}, "
        f"message={idle_message!r},scope='workspace',user_intent='human requested peer messaging qualification', "
        "correlation_id='lifecycle-peer-idle'), then finish. Report its later ACK without replying.",
        "message_agent",
    )
    idle_ack = evidence.observation(idle["invocation"])
    evidence.require(
        lambda: any(
            row["message_id"] == idle_ack["message_id"] and row["delivered_at"]
            for row in evidence.mail(call, peer)
        ),
        "idle peer automatically woke without a user post",
    )
    evidence.require(
        lambda: len(evidence.tool_events(proof, peer, "message_agent")) == 2,
        "idle peer model sent exactly one second reply",
    )
    evidence.require(lambda: evidence.settled_turn(call, peer), "peer idle wake settled")
    evidence.require(
        lambda: evidence.settled_turn(call, sender), "sender second reply wake settled"
    )
    assert len(evidence.mail(call, peer)) == 2 and len(evidence.mail(call, sender)) == 2
    for sid in (peer, sender):
        assert not evidence.tool_events(proof, sid, "spawn_agent_task", complete=False)
        assert not evidence.tool_events(proof, sid, "spawn_agents_parallel", complete=False)
    assert len(evidence.tool_events(proof, peer, "shell_bash")) == 1
    assert not call("GET", f"/v1/sessions/{peer}").get("parent_session_id")
    for sid in (sender, peer):
        evidence.save_session(call, output, sid)
    messages = json.dumps(common.session_messages(call, peer), ensure_ascii=False)
    assert busy_message in messages and idle_message in messages
    row = {
        "pass": True,
        "sender": sender,
        "peer": peer,
        "discovery_and_busy_send": discovery,
        "busy_ack": ack,
        "busy_reply": reply_ack,
        "idle_send": idle,
        "idle_ack": idle_ack,
        "busy_message": busy_message,
        "idle_message": idle_message,
    }
    common.dump_json(output / "verdict.json", row)
    print("LIVE busy/idle peer messaging passed", flush=True)
    return row
