"""Conversation runtime tools every agent has: tasks, questions, planning state."""

from clio_agent.sdk import native_tool

# Shared task controls (query, observe, wait, cancel, collect) and agent controls.
query_tasks = native_tool("query_tasks")
observe_tasks = native_tool("observe_tasks")
wait_tasks = native_tool("wait_tasks")
cancel_tasks = native_tool("cancel_tasks")
get_task_result = native_tool("get_task_result")
send_agent_message = native_tool("send_agent_message")
pause_agent = native_tool("pause_agent")
resume_agent = native_tool("resume_agent")
restart_agent = native_tool("restart_agent")

# Questions, planning state and artifacts.
ask_user = native_tool("ask_user")
plan_exit = native_tool("plan_exit")
write_todos = native_tool("write_todos")
create_artifact = native_tool("create_artifact")
raise_alert_card = native_tool("raise_alert_card")
recall_context = native_tool("recall_context")
load_skill = native_tool("load_skill")

# Goals, loops and schedules.
set_goal = native_tool("set_goal")
wait_for = native_tool("wait_for")
goal_status = native_tool("goal_status")
set_loop = native_tool("set_loop")
loop_wakeup = native_tool("loop_wakeup")
cron_create = native_tool("cron_create")
cron_list = native_tool("cron_list")
cron_delete = native_tool("cron_delete")

# Uploaded workspace resources.
workspace_resource_list = native_tool("workspace_resource_list")
workspace_resource_inspect = native_tool("workspace_resource_inspect")
workspace_resource_wait = native_tool("workspace_resource_wait")
workspace_resource_read = native_tool("workspace_resource_read")
workspace_resource_search = native_tool("workspace_resource_search")
workspace_resource_structure = native_tool("workspace_resource_structure")

# Provider model catalogs (the update-models skill).
refresh_provider_models = native_tool("refresh_provider_models")
