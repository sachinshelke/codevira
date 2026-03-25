import subprocess
import sys
import os

try:
    url = os.environ.get("MY_GIT_URL")
    subprocess.run(["git", "remote", "set-url", "origin", url], check=True)
    
    out = subprocess.run(["git", "push", "-u", "origin", "feat/enhanced-graph-indexing"], capture_output=True, text=True)
    print("STDOUT:", out.stdout)
    print("STDERR:", out.stderr)
    
except Exception as e:
    print(f"Failed to push: {e}")
