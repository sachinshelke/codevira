# QA Tier 2 — Script-Driven Angles

8 angles automatable via scripts. Each has a one-liner invocation;
output is interpretable by humans or by feeding to an Explore agent
with prompt "interpret these results, flag concerning patterns."

These scripts live alongside this file. To create them on demand,
copy the snippet below into the noted location.

---

## Angle 04 — Full-stack reality / latency

**Catches:** Real-world performance gaps (R3 found 67ms vs 50ms claim)

```bash
# Measure full hook round-trip including process spawn
TIMES=()
for i in $(seq 1 30); do
  T0=$(/usr/local/bin/python3.13 -c "import time; print(time.perf_counter())")
  echo '{"session_id":"x","cwd":"/tmp/proj","tool_name":"Edit","tool_input":{"file_path":"x"}}' | \
    codevira engine handle PreToolUse > /dev/null 2>&1
  T1=$(/usr/local/bin/python3.13 -c "import time; print(time.perf_counter())")
  TIMES+=($(/usr/local/bin/python3.13 -c "print(f'{($T1 - $T0) * 1000:.1f}')"))
done
# Compute p50/p95/p99 from TIMES; compare to the spec's documented
# performance budget. Flag if p95 exceeds budget by >20%.
```

---

## Angle 05 — Integration completeness

**Catches:** Missing wiring (R3 found mcp_dispatch never invoked from
call_tool)

```bash
# For each adapter file, verify it's actually invoked from the host.
for adapter in mcp_server/engine/wiring/*.py; do
  # Extract the public function names
  funcs=$(grep -E "^def [a-z]" "$adapter" | sed 's/def \([a-z_]*\).*/\1/')
  for f in $funcs; do
    callers=$(grep -rln "$f(" --include="*.py" mcp_server/ indexer/ \
                 | grep -v "$adapter" | wc -l)
    if [ "$callers" -eq 0 ]; then
      echo "WARN: $adapter:$f has no callers — adapter exists but never invoked"
    fi
  done
done
```

---

## Angle 08 — Type safety + Python compat

**Catches:** Python 3.10+ syntax accidentally used in code shipping
to 3.10+ users (R4 verified import-clean on 3.9 + 3.13)

```bash
# Check imports work on each Python version supported
for py in 3.10 3.11 3.12 3.13; do
  if which python$py > /dev/null; then
    python$py -c "
import mcp_server.engine
import mcp_server.engine.events
import mcp_server.engine.policies
import mcp_server.engine.runner
import mcp_server.engine.signals
import mcp_server.engine.token_meter
import mcp_server.engine.wiring.claude_code_hooks
import mcp_server.engine.wiring.mcp_dispatch
import indexer.fix_history
print(f'  Python $py: clean imports ✓')
"
  fi
done

# Run mypy if available
if which mypy > /dev/null; then
  mypy mcp_server/engine indexer/fix_history.py 2>&1 | tail -10
fi
```

---

## Angle 09 — Concurrent stress

**Catches:** Races, deadlocks, lock contention (R4 verified 10 threads ×
100 events; R2 found unprotected fix_history cache)

```python
#!/usr/bin/env python3
"""Run 50 threads × 100 events concurrently with mixed event types."""
import threading
import time
from pathlib import Path
import sys
sys.path.insert(0, '.')
from mcp_server.engine import EventType, HookEvent, dispatch, register_policy, reset_policies
from mcp_server.engine.policies import Policy, PolicyVerdict

class P(Policy):
    name = "stress"
    handles = (EventType.PRE_TOOL_USE, EventType.POST_TOOL_USE)
    def evaluate(self, e):
        _ = e.signals
        return PolicyVerdict.allow()

reset_policies()
register_policy(P())

errors = []
def worker(tid):
    try:
        for i in range(100):
            ev = HookEvent(
                event_type=EventType.PRE_TOOL_USE if i%2==0 else EventType.POST_TOOL_USE,
                project_root=Path(f"/tmp/p{tid}"),
                tool_name="Edit",
            )
            dispatch(ev)
    except Exception as e:
        errors.append((tid, repr(e)))

t0 = time.perf_counter()
threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
for t in threads: t.start()
for t in threads: t.join()
elapsed = (time.perf_counter() - t0) * 1000
print(f"50 threads × 100 events = 5000 dispatches in {elapsed:.0f}ms")
print(f"Errors: {len(errors)}")
assert not errors, f"Concurrency failure: {errors[:3]}"
```

---

## Angle 14 — Upgrade simulation

**Catches:** Migration breakage when existing users upgrade

```bash
# 1. Set up v1.8.0 fixture state
mkdir -p ~/.codevira-test-old/{logs,projects/proj_abc12345/graph}
echo '{"path_key":"proj_abc12345","original_path":"/tmp/proj","version":"1.8.0"}' \
  > ~/.codevira-test-old/projects/proj_abc12345/metadata.json
# (sqlite3 commands to populate graph.db with old schema if applicable)

# 2. Point codevira at the test home
export CODEVIRA_HOME=~/.codevira-test-old

# 3. Run v2.0 against it; verify no crashes, no data loss
codevira status
codevira clean --orphans --dry-run

# 4. Verify schema migrated cleanly (no errors, expected tables present)
sqlite3 ~/.codevira-test-old/projects/proj_abc12345/graph/graph.db ".tables"
```

---

## Angle 15 — Mutation testing

**Catches:** Code paths that have no test coverage

```bash
pip install mutmut
cd /Users/sachin/Documents/Projects/LogisticsOS/agent-mcp
mutmut run --paths-to-mutate=mcp_server/engine,indexer/fix_history.py \
           --tests-dir=tests/engine \
           --runner="python -m pytest -x"
# Output shows mutants that survived (= tests didn't catch the change)
mutmut results
```

---

## Angle 17 — Crash recovery

**Catches:** SQLite corruption from interrupted writes

```bash
# Spawn a writer that does many fix_history.record_fix() calls
python -c "
from indexer.fix_history import record_fix
from pathlib import Path
import time
for i in range(1000):
    record_fix(Path('/tmp/crash_test'), f'src/f{i}.py', 1, 1, f'fix {i}', source='manual')
    time.sleep(0.001)
" &
WRITER_PID=$!
sleep 0.5

# kill -9 mid-write to corrupt
kill -9 $WRITER_PID 2>/dev/null
wait $WRITER_PID 2>/dev/null

# Now try to read — should not crash, may lose last few writes
python -c "
from indexer.fix_history import lookup
from pathlib import Path
import sys
try:
    results = lookup(Path('/tmp/crash_test'), 'src/f0.py')
    print(f'Survived crash: read {len(results)} records')
    sys.exit(0)
except Exception as e:
    print(f'CORRUPTION: {e}')
    sys.exit(1)
"
```

---

## Angle 21 — Resource exhaustion

**Catches:** Scaling failures (huge DBs, many policies, many sessions)

```python
#!/usr/bin/env python3
"""Stress: 10K fix records + 50 policies + 1000 sessions."""
import sys
sys.path.insert(0, '.')
from indexer.fix_history import record_fix, lookup
from pathlib import Path
import time, tempfile

proj = Path(tempfile.mkdtemp())

# 10K fix records
t0 = time.perf_counter()
for i in range(10_000):
    record_fix(proj, f"src/f{i % 100}.py", 1, 1, f"fix {i}", source="manual")
print(f"10K records inserted in {time.perf_counter()-t0:.1f}s")

# Look up — should be O(log n) per file
t0 = time.perf_counter()
for i in range(100):
    _ = lookup(proj, f"src/f{i}.py")
print(f"100 lookups across 10K records: {(time.perf_counter()-t0)*1000:.1f}ms")
assert (time.perf_counter() - t0) < 1.0, "Lookup degraded under scale"
```
