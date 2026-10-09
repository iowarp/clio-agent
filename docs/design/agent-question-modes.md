# Agent questions with optional A2UI content

The question owns its answer, expiry, dismissal, and conversation. A2UI supplies
optional visual context or form controls within that question, using the existing
catalog and resource references.

`ask_user` accepts `response_mode="blocking"` (the default) when the agent needs
an answer to continue. The question appears immediately in the composer tray.
`response_mode="async"` leaves the agent running: the transcript shows a compact
question entry that opens a dialog. Both modes appear in the open-question control
at the bottom right of the current conversation and in observability.

For example, an agent can ask which of three directions to pursue while showing
an image, then separately ask what X means with a free-text field. The native
question's options and answer field remain available alongside its visual context.

To add a visual, create an A2UI surface in the same conversation and pass its
`surface_id` to `ask_user`. For an A2UI form, declare the action that submits its
answer with `answer_action` (default `question.submit`). Each open visual question
owns its surface; text-only questions can omit the surface reference. The action's structured
context travels to the agent with the answer. An empty optional action uses that
default too. Filters, sliders, tabs, inspection,
and other actions do not settle the question. Existing questions without this
metadata retain their legacy action correlation.

The native question UI explains whether work pauses and how the answer is delivered.
Agent-authored A2UI content provides context and controls rather than duplicating
those lifecycle instructions.

Async answers enter the ordinary durable message queue, behind earlier queued
messages. They do not interrupt current work. Text, choices, files, and references
retain the same question identity; files and references use the normal composer
upload, authorization, and retention path. A full queue leaves the question open
so the person can retry. Retries on the queued-message path are idempotent;
a second answer is refused. Queue promotion rechecks references and waits while
a blocking question remains pending. Native replies retain the source turn's
model and per-message behavior, including planning and reasoning settings.

Dragging the bottom-anchored tray upward increases its height. Expanded questions
use a centered, scrollable dialog at the selected transcript width. Questions from
other conversations remain in navigation instead of appearing in this composer.
