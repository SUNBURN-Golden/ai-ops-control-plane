# Security Boundaries

- Grok Bots under one Cursor user share the box filesystem and browser logins. Separate chat UIs are not isolation.
- Least privilege: Marketing must not receive full case dossiers; Dev must not receive client originals for unrelated coding.
- Secrets stay out of chat and out of this control-plane repo.
- OneDrive: read only — never mutate.
- True isolation (if ever required) needs separate Cursor accounts / credential boundaries — not more bots on the same box.
