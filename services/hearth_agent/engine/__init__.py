"""
The agent engine — agent-agnostic runtime that turns a remediation job into
normalized events and persists session metadata.

- dispatcher    : routes a job (or a chat turn) to the active agent's adapter
- stream_events : the event vocabulary every adapter's ``stream()`` and ``chat()`` speak
- chat_bridge   : carries results of web-app-run tools back into a live chat turn
- sessions_io   : the session index (one row per job's agent session)
- usage_loader  : thin alias resolving the active agent's usage capability

None of these name a specific agent; they resolve behavior via the adapters
capability loader.
"""
