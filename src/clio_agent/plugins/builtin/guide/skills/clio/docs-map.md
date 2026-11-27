# CLIO documentation map

One line per page of the CLIO docs that ship with this version: its path, title and
description, grouped as the docs sidebar groups them. Load a page with
`load_skill("clio", files=["docs/<path>.md"])`. The site address of a page is
https://clio.iowarp.ai/docs/<path>/ (an `index` page is its folder).

## Get started

- `docs/index.md`: **Start here**. A practical introduction to CLIO for researchers, from a first question to a result you can inspect and share.
- `docs/install.md`: **Install CLIO**. Install CLIO on Windows, macOS or Linux, as the desktop app, from a terminal, on a remote computer, or with your AI assistant.
- `docs/install-with-ai.md`: **Install with your AI assistant**. Give your AI assistant one link and it installs CLIO for you, on Windows, macOS or Linux.
- `docs/connect-a-model.md`: **Choose a model**. Choose where CLIO gets its language model, from a local server to a cloud API or a subscription.
- `docs/examples/check-a-dataset.md`: **Your first analysis: check a small dataset**. Bring a small CSV into CLIO, ask one answerable question and inspect the evidence before keeping the result.
- `docs/workspace-panels.md`: **Your workspace: conversation, data and results**. Find your files, open a result beside the conversation and keep track of work that needs you.
- `docs/versions.md`: **Features and versions**. Understand why a guide can look different from your installed app and how to check what is available.
- `docs/ask-clio.md`: **Ask CLIO how to use CLIO**. Ask how to do something in CLIO, or whether it can, and get the answer from the documentation of the version you have, with its screenshots.

## Bring your data

- `docs/files/index.md`: **Bring your data into CLIO**. Choose how to attach files, refer to existing work or connect a source.
- `docs/files/attach.md`: **Attach files and folders**. Add new material to a message, inspect it before sending, and undo a mistaken attachment.
- `docs/files/references.md`: **Reference material with @**. Find existing files and evidence, insert a reference at the cursor, and inspect it before sending.
- `docs/files/connect.md`: **Connect a data source**. Sign in once, choose a source for a workspace, and decide which files to add.
- `docs/files/ndp.md`: **Work with National Data Platform**. Search NDP datasets, connect existing remote APIs, and keep data and stream captures with your analysis.
- `docs/files/ndp-streams.md`: **Sample and analyze NDP streams**. Ask CLIO to analyze stream observations, check available history and explain the limits of the evidence.
- `docs/files/download-or-link.md`: **Download a copy or link to originals**. Choose where files live, whether you can edit them, and whether changes reach the source.
- `docs/files/knowledge.md`: **Explore research notes**. Create a new Obsidian-compatible vault, attach existing notes, or let CLIO manage Obsidian Sync as a knowledge source.
- `docs/files/manage.md`: **Manage and reuse sources**. Reuse folders, remove draft attachments, unlink data, and sign out without confusing their effects.

## Work with CLIO

- `docs/sessions.md`: **Start and steer a conversation**. Ask a question, set the scope, review decisions and keep work in one conversation.
- `docs/tasks/index.md`: **Background work**. Keep working while tools and subagents run, follow their progress, and receive their results automatically.
- `docs/tasks/observe-and-wait.md`: **Observe and wait**. Query task status, follow incremental output, and collect results across different kinds of background work.
- `docs/tasks/subagents.md`: **Subagent controls**. Inspect a child's assignment, pause and resume it, cancel its descendants, or create a linked restart.
- `docs/tasks/messages.md`: **Messages and feedback**. Deliver feedback at the next model iteration, wake an idle recipient, and coordinate with an authorized session.
- `docs/tasks/tools-and-sources.md`: **Task-capable tools and sources**. Understand background MCP calls, downloads, folder indexing, shell commands, scripts and subagent work.
- `docs/tasks/session-coordination.md`: **Session coordination**. Durable session run handles, authorized peer observation and independent completion notifications.
- `docs/tasks/recorded-conversations.md`: **Watch agents work together**. Read real CLIO conversations where independent agents inspect, message and review each other, and where an agent delegates to read-only roles or forks itself.
- `docs/questions.md`: **Answering CLIO's questions**. See the different question types, add context to a choice, and find questions that still need your answer.
- `docs/web-search.md`: **Search the web**. How CLIO searches the web and reads pages, which services it uses, and how to approve, order, limit or upgrade them.
- `docs/literature.md`: **Find papers and review the literature**. Search scholarly sources for papers, preprints, reports and datasets, follow references and citations, and ask for a literature review with verified sources.
- `docs/deep-research.md`: **Run deep research**. Ask a question in Deep research mode to get a sourced report whose claims an independent critic checked against the pages they quote.
- `docs/browser.md`: **Use the browser with CLIO**. Watch and use the web pages CLIO opens, sign in to sites yourself, choose which sites it may load, and pick the browser it runs.
- `docs/apps.md`: **Apps**. See a connector's live view in the conversation, open it in a tab, show it to the agent, comment on it, link it to a table in a dashboard, and reopen it later.
- `docs/kicad.md`: **Design a circuit board with KiCad**. Turn on the KiCad connector, let the agent design a board from a brief, and watch and steer it in the board, schematic, BOM, checks, stackup and parts views.
- `docs/ibm-quantum.md`: **Quantum programs with IBM Quantum**. Turn on the IBM Quantum connector, let the agent write and simulate Qiskit programs on this machine, see circuits, layouts, results and devices in views, and run on IBM quantum computers within your time.
- `docs/scripts.md`: **Scripts and background work**. How the agent writes Python scripts that call its tools, how you approve them, and how a script keeps running in the background.
- `docs/live-sources.md`: **Watch something live**. Keep an eye on a job, an instrument or a device, with a dashboard that updates by itself while it is open.
- `docs/following-work.md`: **Follow work in progress**. See what is running, what is waiting for you and which results are ready to inspect.
- `docs/flows.md`: **Flows and workflows**. Run several agents on one question as a council, an arena, or a workflow of steps; follow the run, steer it, and decide which flows may run.
- `docs/subagents.md`: **Subagents**. The subagents CLIO can hand work to, a copy of the agent and the read-only explore, planner and review roles, and the forks an agent makes to try another direction.
- `docs/commands.md`: **Commands and @ mentions**. Run a command with /, bring in a subagent, workflow, flow or tool server with @, and write your own commands.
- `docs/science-log.md`: **Science log**. Ask CLIO to answer a question by experiment, then follow its hypotheses, measurements, evidence and conclusions in the Science log.
- `docs/models-and-usage.md`: **Models and usage**. See what each connected account has used and has left, let CLIO run parts of the work on other models, and decide which accounts it may spend.
- `docs/injections.md`: **Updates during a conversation**. Understand reminders, background results and recovered context that appear while CLIO works.
- `docs/long-running-work.md`: **Continue work over time**. Give CLIO a finishing condition, monitor a running job or schedule a future check, then inspect and stop that work.
- `docs/permissions.md`: **Choose what CLIO may do**. Review requested actions, choose an approval policy and understand access limits.

## Review and share results

- `docs/evidence.md`: **Check answers and sources**. Review the source data, tool results and saved versions behind a finding.
- `docs/working-with-files.md`: **Open and edit documents**. Read a file beside the conversation, request a revision and keep the editable original.
- `docs/comments.md`: **Comment on results**. Mark the part of a document, figure, web page or answer you mean, and say what to change.
- `docs/papers.md`: **Write a paper**. Ask for a LaTeX paper, read its source beside the built PDF, comment on either and follow its versions.
- `docs/dashboards.md`: **Explore charts and dashboards**. Read a finding alongside linked plots, maps and the underlying table.
- `docs/best-of-n.md`: **Compare alternative answers**. Compare independent drafts with Best of N, check the criteria, and choose one to continue or improve with feedback.
- `docs/exports.md`: **Save and share your work**. Download a file, export a report or share a conversation with its recorded evidence.

## Find and revisit work

- `docs/search.md`: **Search your work**. Find matching conversation passages and files, or ask the read-only CLIO Search assistant.
- `docs/long-term-memory.md`: **Long-term memory**. What CLIO keeps about you and your projects across conversations, how it learns it, and how you review, correct and remove it.
- `docs/memory.md`: **What CLIO remembers**. Find earlier decisions and understand what a conversation summary can leave out.
- `docs/branch-and-rewind.md`: **Try another approach**. Keep the original conversation while exploring another direction, or rewind with care.

## Worked examples

- `docs/examples/index.md`: **Worked examples**. Choose a complete investigation with a starting point, a request to try and checks for the result.
- `docs/examples/ground-motion.md`: **Explore ground motion near Palm Springs**. Ask CLIO for the GNSS stations nearest Palm Springs, choose one on a map, chart its motion, and check the evidence behind the result.
- `docs/examples/ndp-earthscope.md`: **Analyze EarthScope data from NDP**. Investigate a scientific question with documented observations, checked provenance and explicit historical coverage.
- `docs/examples/ndp-science.md`: **Scientific questions for NDP**. Explore scientific investigations with NDP data, follow-up questions, interactive results and downloadable conversations.
- `docs/examples/hdf5-paraview.md`: **Explore a hurricane in HDF5 data**. Use public ERA5 reanalysis of Hurricane Helene to inspect HDF5 files, track the storm, see its clouds in ParaView and share a time-linked dashboard.
- `docs/examples/branched-study.md`: **A branched study**. Ask a question that needs experiments, then follow the study in the Science log as parallel branches test competing answers.
- `docs/examples/hybrid-qml.md`: **A hybrid quantum classifier**. Train a hybrid quantum-classical classifier beside a classical baseline, then see what a noisy quantum device does to its predictions, image by image.
- `docs/examples/deep-research.md`: **Research a question in depth: lossy compression for HDF5**. Run one deep research question from start to finish, watch the researchers and their checks, and read the report knowing what each part was checked against.
- `docs/examples/research-notes.md`: **Find a decision in research notes**. Use a fictional field notebook to find a calibration decision, inspect its source and bring it into a conversation.
- `docs/examples/reports-and-slides.md`: **Create a report and presentation**. Use fictional sensor notes to generate, review and revise editable documents.

## Customize CLIO

- `docs/connectors.md`: **Connectors**. Find connectors and plugins, bring them into the conversations that need them, let the agent offer the one a request needs, and add your own MCP server.
- `docs/agents-and-plugins.md`: **Agents and plugins**. Choose the agent for a conversation, add or remove its plugins, save your own agent, and read the prompt it receives.
- `docs/capture.md`: **Turn your work into a plugin**. Use /capture to turn what you did in a conversation into a plugin you can reuse, and /optimize to improve a plugin from the conversations that used it.
- `docs/extensions.md`: **MCPs, skills and plugins**. Discover extensions, connect an MCP, inspect skill files, and combine them into a plugin you can reuse.
- `docs/marketplaces/index.md`: **Plugin marketplaces**. Where agents and plugins come from, what the shipped clio marketplace holds, and how Claude Code marketplaces fit in.
- `docs/marketplaces/manage.md`: **Add and manage marketplaces**. Add a marketplace from a folder or a git repository, trust it, update it, remove it, and check its plugins.
- `docs/mcp-servers.md`: **Connect tools and services**. Add capabilities from a trusted tool server, with technical setup for your administrator.
- `docs/widgets.md`: **Widget gallery**. Explore scientific data through CLIO's interactive views.

## Build with the SDK

- `docs/sdk/index.md`: **Build agents, plugins and flows**. How CLIO agents are made from plugins, what each piece of a plugin does, and how a session turns them into a working agent.
- `docs/sdk/build-a-plugin.md`: **Build your first plugin**. Build a spectroscopy plugin with a tool, a skill, a prompt fragment, a hook, a subagent and a flow, add an agent that carries it, then check, install, trust and use it.
- `docs/sdk/flows.md`: **Write a flow**. Write a procedure in Python that calls tools, subagents and agents, in parallel or in sequence, asks the person, and continues after a restart.
- `docs/sdk/workflows.md`: **Workflows as data**. Write a procedure as a list of steps and what each waits on, let editors extend it while it runs, and start it from a flow or let the agent start it.
- `docs/sdk/available.md`: **What's available**. The plugins built into CLIO and in the clio marketplace, the flows you can run or call, which plugin brings each tool, and the agents you can build on.
- `docs/sdk/plugins.md`: **Plugin reference**. Every file a CLIO plugin may contain, the fields each accepts, and the rules CLIO checks when it loads and resolves a plugin.
- `docs/sdk/connectors.md`: **Write a connector entry and a following plugin**. The connector entry format, the plugin field that ties a plugin to a connector, and the CLIO-specific parts of an MCP App.
- `docs/sdk/agents.md`: **Agent reference**. Define an agent as a Python class, refer to plugins by id and version range, choose its flow and model requirements, and read how CLIO resolves it.
- `docs/sdk/reference.md`: **SDK reference**. Every class and function in clio_agent.sdk, the flow runtime's ctx, call results and the typed errors a flow can see.

## Help and privacy

- `docs/troubleshooting.md`: **Troubleshooting**. Check your install, read the logs, and fix the problems people hit most often.
- `docs/privacy.md`: **Data and network**. What CLIO runs on your machine, exactly what can leave it, and how to limit it.
- `docs/uninstall.md`: **Uninstall**. Remove CLIO from your machine, and find the few things it leaves behind.

## Technical reference

- `docs/server-setup.md`: **Server and terminal setup**. Technical installation options for lab administrators and people running CLIO from a terminal or server.
- `docs/managed-infrastructure.md`: **Managed infrastructure**. Which model servers and services CLIO can install on Linux, Windows and macOS, with or without a GPU, and what each computer needs.
- `docs/cli.md`: **Command line**. Start, stop, and inspect the CLIO backend with the clio command, and what differs on Windows.
- `docs/configuration.md`: **Configuration**. Where CLIO keeps its files, how config.yaml and environment variables combine, and the settings that matter most.
- `docs/hooks.md`: **Hooks**. Run your own checks at key points in a CLIO session, such as before a tool runs or before a request goes to the model.
- `docs/background-work-reference.md`: **Background work: limits and API reference**. Pursue an objective, monitor a job, or schedule a future turn, with controls in Work.
- `docs/widgets/components.md`: **Component catalog**. Every CLIO widget and its agent-facing inputs.
- `docs/contributing.md`: **Contributing**. Set up a development environment, run the checks, and send a change to CLIO.

## Other pages

- `docs/widgets/action-card.md`: **Action card**. A short pitch with up to six labeled actions.
- `docs/widgets/approval.md`: **Approval**. A gated decision with one to four actions.
- `docs/widgets/artifact.md`: **Artifact**. A reference card for one registered artifact.
- `docs/widgets/button.md`: **Button**. A clickable control wrapping one child, firing an action.
- `docs/widgets/callout.md`: **Callout**. A short flagged note distinct from plain text (info/warning/error severity).
- `docs/widgets/carousel.md`: **Carousel**. Browse child views one at a time, such as images, captioned instructions, or video.
- `docs/widgets/chart.md`: **Chart**. Any chart over tabular rows: a preset or an Altair/Vega-Lite spec.
- `docs/widgets/check-box.md`: **Check Box**. A single labeled boolean toggle.
- `docs/widgets/choice-picker.md`: **Choice Picker**. A single- or multiple-selection picker; bind value to the path an action reads.
- `docs/widgets/code.md`: **Code**. Syntax-highlighted source, inline or from a referenced file.
- `docs/widgets/column.md`: **Column**. A vertical layout container for child components.
- `docs/widgets/data-table.md`: **Data table**. Tabular rows, inline or from a referenced dataset.
- `docs/widgets/date-time-input.md`: **Date Time Input**. A date, time, or date-time input with an ISO 8601 value; enable the needed parts.
- `docs/widgets/diff.md`: **Diff**. A unified diff for one path, inline or from a referenced file.
- `docs/widgets/divider.md`: **Divider**. A horizontal or vertical rule separating layout regions.
- `docs/widgets/frame.md`: **Frame**. A titled container around one child component.
- `docs/widgets/grid.md`: **Grid**. A fixed-column grid layout container for child components.
- `docs/widgets/icon.md`: **Icon**. A named or SVG icon glyph.
- `docs/widgets/image.md`: **Image**. A raster or vector image by URL, sized to a preset variant.
- `docs/widgets/interaction.md`: **Human-agent interaction**. Carry a selection or a visual region into the next question.
- `docs/widgets/linked-data.md`: **Linked data**. Select once and find the same observation across views.
- `docs/widgets/list.md`: **List**. A vertical or horizontal list of child components, optionally templated from bound data.
- `docs/widgets/map.md`: **Map**. Interactive points, ordered trajectories, or GeoJSON geometry on a map.
- `docs/widgets/mcp-app.md`: **App view**. An MCP App view opened by a tool call, with its state linked to the surface's data model.
- `docs/widgets/mermaid.md`: **Diagram**. Mermaid diagrams: workflows, protocols, ER/class models, states, git histories, grammars, trees and 33 supported families. Load this component for syntax and examples.
- `docs/widgets/mesh-viewport.md`: **3D viewport**. An orbitable 3D view of a registered standard mesh or a CLIO FEA mesh with optional result fields.
- `docs/widgets/message-draft.md`: **Message draft**. Editable alternatives for an email, chat message, or text, with copy and open actions.
- `docs/widgets/metric.md`: **Metric**. One labeled number, optionally with a unit and trend.
- `docs/widgets/modal.md`: **Modal**. A dialog opened by a trigger component, showing one content component.
- `docs/widgets/numeric-slider.md`: **Numeric slider**. A fractional-step numeric control, or a two-value range, with typed value boxes.
- `docs/widgets/progress.md`: **Progress**. A bounded or indeterminate operation's progress.
- `docs/widgets/raster-viewport.md`: **Raster viewport**. An interactive viewport for a registered grid (one 2-D field, or one frame of a time series), with sampled values, colour scale, and region reference.
- `docs/widgets/row.md`: **Row**. A horizontal layout container for child components.
- `docs/widgets/slider.md`: **Slider**. A whole-number range control (see clio.slider.v1 for fractional steps).
- `docs/widgets/status.md`: **Status**. A single labeled state, e.g. a running step.
- `docs/widgets/steps.md`: **Steps**. An interactive protocol or recipe with checkable actions, timers, and separately scaled ingredients.
- `docs/widgets/tabs.md`: **Tabs**. A tabbed switcher between named child components.
- `docs/widgets/text-field.md`: **Text Field**. A single- or multi-line text input.
- `docs/widgets/text.md`: **Text**. A run of text, plain or headed (h1-h5, caption, body).
- `docs/widgets/video.md`: **Video**. A video from a registered file, with playback and full-screen controls.
- `docs/widgets/weather.md`: **Weather**. Current weather and hourly or daily forecasts for a field site or location.
- `docs/widgets/workflow.md`: **Workflow**. A step/edge graph showing a multi-step plan's progress.
