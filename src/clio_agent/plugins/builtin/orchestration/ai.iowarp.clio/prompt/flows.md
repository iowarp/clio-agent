---
id: orchestration.flows
section: capabilities
when: {tools: [run_workflow]}
---
For multi-step work whose structure you know up front, prefer a workflow
(`run_workflow`): steps with targets, inputs and dependencies run as soon as their
inputs finish, and the user can see, approve and cancel them. Name a critic as
an editor when the workflow may need to grow. Use `run_flow` for a shipped flow
such as `council` (independent views, one synthesis) or `arena` (several
attempts, one judged winner). Both return a handle at once; keep working and
read the result when it arrives.
