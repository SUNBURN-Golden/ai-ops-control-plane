# Security Boundaries

## Shared box = shared files and logins

Grok Bots under one Cursor user share:

- The **same box filesystem** (`/workspace`, agent data, downloads)
- **Browser logins and cookies** on that machine
- MCP / CLI credentials available in the environment

**Separate chat UIs are not isolation.** A Marketing bot can see files a Legal bot wrote on the box unless process discipline prevents it.

True isolation (if ever required) needs **separate Cursor accounts / credential boundaries** — not more bots on the same box.

---

## Least privilege (process)

- Marketing must not receive full case dossiers
- Dev must not receive client originals for unrelated coding
- Customer bots get CRM/CASE fields needed for the task — not entire OneDrive trees by default
- Prefer redacted excerpts and case ids over dumping originals into chat

---

## Secrets

- Secrets stay out of chat and out of this control-plane repo
- Never commit `.env`, PATs, cookies, or session dumps
- Use env vars already provisioned; never `echo` / print tokens

---

## OneDrive

Absolute **READ ONLY** — never mutate. See `ONEDRIVE_READ_ONLY_POLICY.md`.

---

## ChatGPT ban (until explicit OK)

ChatGPT and similar external AI UIs on the shared box browser:

- **Banned for use** until JunTae gives **explicit OK** for that session/task
- Being logged in is not permission to use
- Prefer Grok Bot teammates + this control plane for org work

---

## Related

- `SYSTEM_CONSTITUTION.md` hard rules 8–10
- `APPROVAL_MATRIX.md` L2/L3
