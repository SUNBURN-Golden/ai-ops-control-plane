# OneDrive — Absolute READ ONLY

Applies to every Bot, Subagent, Worker, Case Worker, Routine, Automation.

## Allowed
- List folders/files
- Read/open file contents
- Search
- Confirm existence/attributes
- Download a copy **outside OneDrive** for analysis (never write the copy back)

## Forbidden
Any create, write, update, delete, move, rename, upload, copy-inside-OneDrive, temp/index/sidecar/log/lock, auto-save via Office edit mode, comment, tag, metadata, sharing, permission, version restore, sync-back, API mutate.

If work seems to need OneDrive write: stop, report to JunTae, do not invent exceptions.
