# Goal receipts in the conversation

The `/goal` command records a service notice with `source: goal` and an explicit
result code (`goal_started`, `goal_stopped`, `goal_rejected` or `goal_command`).
Successful start/stop receipts retain the objective, independently of later
changes to the session's current goal. The API response text remains compatible.

The conversation renders that notice as a Goal entry with a target icon and an
Open Work action. Clicking it navigates to Work; it does not invoke the command
again, restart work or stop a goal. Goal receipts are service events, so they do
not acquire an answer's Done footer. Archives without Work navigation retain a
readable, noninteractive entry.

Older text receipts are projected using their recorded command-result metadata.
Their stored bytes stay intact. Ordinary prose mentioning `/goal` is not
reclassified. The existing notice block carries this distinction in live events
and reloaded messages; no heuristic over arbitrary model text is needed.

Goal summaries in the Work finder and footer use the same target icon and
explicit Goal state. Loops, schedules and todos retain distinct icons. The
footer labels todo counts separately and uses the same control height as its
neighboring status controls.
At narrow widths it omits the duplicate todo count so the Goal state stays
readable; the Work view retains the complete list and counts.
