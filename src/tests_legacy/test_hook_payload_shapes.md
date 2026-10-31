Captured 2026-10-27 from Claude Code 2.1.312 `claude -p` with a logging hook (see docs/SPEC.md §4):
- PreToolUse Bash: tool_input {command, description[, timeout, run_in_background]}
- PreToolUse Edit: tool_input {file_path, old_string, new_string, replace_all}
- PreToolUse Write: tool_input {file_path, content}
- PostToolUse Bash (success): tool_response {stdout, stderr, interrupted, isImage, noOutputExpected}  (no exit code field)
- PostToolUseFailure Bash: {tool_name, tool_input, error: "Exit code 2\n...", is_interrupt}
- UserPromptSubmit: {prompt, permission_mode}
- SessionStart: {source: startup|resume|clear|compact|fork}
- Common: session_id, transcript_path, cwd, hook_event_name, permission_mode, effort; agent_id/agent_type inside subagents
