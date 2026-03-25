import os
import subprocess
import time
from pathlib import Path

print("Starting chaos test on Hash-Based Incremental Indexing & Memory...\n")

# 1. Clean & Initialize project
os.system("rm -rf inc_project")
os.makedirs("inc_project/src", exist_ok=True)
with open("inc_project/src/main.py", "w") as f:
    f.write('def main():\n    print("v1")\n')

subprocess.run(["uv", "run", "codevira", "init"], cwd="inc_project", input=b"y\ninc\npython\nsrc\n.py\n", check=True)
print("-> Initialized codevira in inc_project.")

# Run initial full index to build baseline
subprocess.check_output(["uv", "run", "codevira", "index", "--full"], cwd="inc_project")
print("-> Built baseline code index.")

# 2. Touch the file without changing content
os.system("touch inc_project/src/main.py")

# Run index
out1 = subprocess.check_output(["uv", "run", "codevira", "index"], cwd="inc_project").decode()
assert "No files changed" in out1, f"Expected no changes because hash is same! Output:\\n{out1}"
print("-> Verified hash-based diffing skipped unmodified file.")

# 3. Modify the file content
with open("inc_project/src/main.py", "a") as f:
    f.write('def extra():\n    pass\n')

out2 = subprocess.check_output(["uv", "run", "codevira", "index"], cwd="inc_project").decode()
assert "1 changed file(s)" in out2, f"Expected 1 changed file! Output:\\n{out2}"
print("-> Verified hash-based diffing caught the modified file.")

# 4. Test Memory / Session Logging
print("-> Testing SQLite Session Memory...")
test_script = '''
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
'''
with open("inc_project/test.py", "w") as f:
    f.write(test_script)

subprocess.run(["uv", "run", "python", "test.py"], cwd="inc_project", check=True)

print("\nAll chaos incremental testing passed successfully!")

