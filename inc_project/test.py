
import sys
from mcp_server.tools.search import write_session_log, search_decisions

write_session_log(
    session_id="session-123",
    task="Fix the main function",
    task_type="bugfix",
    files_changed=["src/main.py"],
    decisions=[{"file_path": "src/main.py", "decision": "Added extra() function to handle X", "context": "Because user requested it"}],
    next_steps=["Test extra()"]
)

res = search_decisions("extra() function")
assert len(res["results"]) > 0, "Failed to retrieve decision from SQLite Memory!"
print("Decision found:", res["results"][0]["decision"])
print("SQLite Memory integration is working perfectly!")
