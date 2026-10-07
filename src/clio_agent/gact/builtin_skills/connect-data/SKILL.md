---
name: connect-data
description: Use when the user asks to connect data, or when needed inputs are absent after inspecting supplied references and workspace folders. Connect external sources through CLIO's private setup UI.
---

# Connect data

Start with supplied attachments, references and the current workspace folders.
Inspect the relevant files and their self-description using available file tools.
A primary workspace folder is already input; an empty connected-source list
does not mean its files are missing. If a read fails, diagnose the reported
access or runtime error rather than asking the user to reconnect that folder.

Use this setup workflow when the user explicitly wants to connect a source,
or a bounded workspace inspection finds that the needed data is elsewhere.
Explain what input is missing and ask where it is before requesting setup.

Call `connected_data_status` to present **Connect data** in the main session.
The user opens the same trusted picker available from the composer and Files.
If this tool is absent in a child session, return to the main session for setup.

Briefly explain that the user selects a source and, when required, signs in in
their browser. Sign-in runs independently of the agent; only the outcome and
approved data references are returned. The UI's info icon explains this privacy
boundary. Never ask for passwords, access tokens, authorization codes, callback
URLs, or OAuth client configuration in chat. Never perform sign-in with shell,
HTTP, or browser tools, and never read CLIO's private credential files.

Wait for the user to complete setup; do not repeatedly poll. Call
`connected_data_status` again when they return. Report the actual source status;
registration alone does not mean files are available. Use the approved local
path only after materialization is ready. Disconnected sources cannot access
upstream data; their retained local copies are historical evidence.

Read only is the default. Working copy preserves an immutable baseline and
requires the user to review selected changes before applying them upstream.
Write enabled is offered only where the selected host supports a genuinely
writable folder. Do not substitute synchronization or install filesystem drivers.
Refresh, apply changes, disconnect, remove a copy, and delete upstream data are
different actions. Use the trusted UI for permission changes and reviewed writes.

All paths belong to the connected CLIO named in the picker. To send a desktop
folder to a remote CLIO, use **Upload desktop folder** and preserve its structure.
If a provider reports that distributor setup is missing, report that prerequisite;
do not ask an ordinary user to register an OAuth application.
