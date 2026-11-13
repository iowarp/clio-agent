"""Conversation runtime tools every agent has: tasks, questions, planning state."""

from clio_agent.sdk import native_tool

# Shared task controls (query, observe, wait, cancel, collect) and agent controls. The
# controls an agent reaches for rarely are deferred: named in the prompt, loaded with
# search_tools when needed.
query_tasks = native_tool("query_tasks")
observe_tasks = native_tool("observe_tasks", defer=True)
wait_tasks = native_tool("wait_tasks")
cancel_tasks = native_tool("cancel_tasks", defer=True)
get_task_result = native_tool("get_task_result", defer=True)
send_agent_message = native_tool("send_agent_message")
pause_agent = native_tool("pause_agent", defer=True)
resume_agent = native_tool("resume_agent", defer=True)
restart_agent = native_tool("restart_agent", defer=True)

# Questions, planning state and artifacts.
ask_user = native_tool("ask_user")
plan_enter = native_tool("plan_enter")
plan_update = native_tool("plan_update")
plan_exit = native_tool("plan_exit")
write_todos = native_tool("write_todos")
create_artifact = native_tool("create_artifact")
raise_alert_card = native_tool("raise_alert_card")
recall_context = native_tool("recall_context")
load_skill = native_tool("load_skill")
search_tools = native_tool("search_tools")
# A value one of the person's connectors needs, given in the conversation (CN6).
set_connector_value = native_tool("set_connector_value")
# The pixels and the last report of an app view open in the conversation (CM1).
look_at_view = native_tool("look_at_view", defer=True)
run_code = native_tool("run_code")

# Goals, loops and schedules.
set_goal = native_tool("set_goal")
wait_for = native_tool("wait_for")
goal_status = native_tool("goal_status")
set_loop = native_tool("set_loop")
loop_wakeup = native_tool("loop_wakeup")
cron_create = native_tool("cron_create", defer=True)
cron_list = native_tool("cron_list", defer=True)
cron_delete = native_tool("cron_delete", defer=True)

# Uploaded workspace resources.
workspace_resource_list = native_tool("workspace_resource_list")
workspace_resource_inspect = native_tool("workspace_resource_inspect")
workspace_resource_wait = native_tool("workspace_resource_wait")
workspace_resource_read = native_tool("workspace_resource_read")
workspace_resource_search = native_tool("workspace_resource_search")
workspace_resource_structure = native_tool("workspace_resource_structure")
