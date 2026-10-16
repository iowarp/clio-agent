# Steering during generation and goal evaluation

A steer is accepted immediately and delivered at the next model boundary. Text
already in flight can arrive before that boundary. Once generation finishes,
CLIO checks for steering before starting any tool proposed by that response.
If a steer was delivered, CLIO retains the already visible prose and reasoning,
records the human message after it, and asks the model to replan. It does not
execute or record the discarded tool proposal as a call. An already executing
tool finishes under the existing cancellation policy; steering is not Stop.
Passive child results do not discard a proposal, and configured iteration
limits still apply.

The goal judge uses consumed-steer delivery receipts to interleave human input
with the assistant parts on either side. Creation time alone cannot order a
message that kept streaming across a steer. Missing owners or anchors leave the
human message visible in ordinary message order. Tool call/result pairs share
the public transcript's call-block boundary. Existing bounded tool summaries
and credential redaction remain in effect.

Pending human input defers goal judging and supersedes a verdict that returned
before delivery. Explicit goal limits still stop automatic continuation. An
already queued goal wake is rejected when human input is pending or when a
newer goal evaluation has replaced its recorded iteration. The human turn can
then evaluate the active goal using the corrected conversation.

The UI labels model reasoning as Thinking. A sentence such as “Writing shader
code” is provider prose, not evidence of an executed tool. Tool rows require
recorded tool events. Continued activity is not folded across a delivered steer.
Original message, part and tool identities remain available for inspection.

Adjacent human steers share one sender header in the UI. Each bubble retains its
own identity, timestamp, references, copy/mark controls, and cancellation before
delivery. Assistant activity, a different delivery boundary, or a changed
execution mode starts a new group.

New streamed reasoning passages record their first-delta timestamp and their
closing timestamp, with a monotonic elapsed duration. This is CLIO-observed phase
time, not provider compute or scheduling time before the first delta. The live
completion event and persisted part carry the same timing. Tool execution keeps
its own start/end clock. Older or batch-only reasoning without recorded timing
shows no invented duration.
