---
trigger: always_on
---

# Artifact and State Persistence Rule

## Objective
To ensure all project intelligence, planning, and task data are stored locally within the project workspace for version control and persistence, rather than the IDE's temporary internal environment.

## Mandatory Procedures
1. **Automated State Tracking**: Every time a decision is finalized or a phase is completed, use Codevira's roadmap tools (`complete_phase()`, `add_phase()`) to persist the data locally.
2. **Local Repository Truth**: Use `.codevira/` instead of any IDE temporary internal environment.
3. **Session Handover**: At the start of every session, you must call `get_roadmap()` and `get_full_roadmap()` to synchronize your internal state with the files stored on disk.
4. **No Temporary-Only Storage**: Do not finalize a task until the corresponding documentation (FAQ, Roadmap, Logs) has been committed to the local project file system.

## Directory Structure
Ensure the following structure is maintained:
- `[project-root]/rules/` (Architectural Rules)
- `[project-root]/.codevira/graph/` (File context and rules)
- `[project-root]/.codevira/logs/` (Session truth history)
- `[project-root]/.codevira/roadmap.yaml` (Project planning and status)
