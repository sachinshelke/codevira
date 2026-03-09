---
trigger: always_on
---

# Artifact and State Persistence Rule

## Objective
To ensure all project intelligence, planning, and task data are stored locally within the project workspace for version control and persistence, rather than the IDE's temporary internal environment.

## Mandatory Procedures
1. **Physical File Creation**: Every time a Plan, Task List, or Workflow is generated or updated, you must write a physical copy to the `./.agent/artifacts/` directory.
2. **Naming Convention**: 
   - Implementation Plans: `./.agent/artifacts/plan_[task_name].md`
   - Task Lists: `./.agent/artifacts/tasks.md`
   - Design/Visual Artifacts: Save descriptions/references in `./.agent/artifacts/media_log.md`
3. **Session Handover**: At the start of every session, you must read the contents of the `./.agent/` directory to synchronize your internal state with the files stored on disk.
4. **No Temporary-Only Storage**: Do not finalize a task until the corresponding documentation has been committed to the local project file system.

## Directory Structure
Ensure the following structure is maintained:
- `[project-root]/.agent/rules/` (For these instructions)
- `[project-root]/.agent/artifacts/` (For plans and task states)
- `[project-root]/.agent/workflows/` (For custom slash commands)
