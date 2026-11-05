# Plan: MapleWave Agent Website

## Overview

Create a minimal branded single-page website that embeds the MapleWave Billing Adjustment Assistant chat widget from watsonx Orchestrate. The site lives in a new `website/` folder at the project root. A companion Python script lets you spin up a local HTTP server with one command.

No framework, no build step, no dependencies — just a plain HTML file and a Python script.

---

## Sub-Tasks

### 1 — Create `website/index.html`

**Intent**  
Deliver the actual web page: a simple branded shell that loads the watsonx Orchestrate chat widget via the provided embed script.

**Expected Outcomes**  
- `website/index.html` exists and opens cleanly in a browser.  
- The page shows a header with the MapleWave name and "Billing Adjustment Assistant" description.  
- The chat widget loads and is functional below the header.  
- The layout is responsive (works on desktop and a typical laptop screen).

**Todo List**  
- [ ] Create `website/` directory.  
- [ ] Write `website/index.html` with:
  - A `<head>` section: charset, viewport meta, title ("MapleWave — Billing Adjustment Assistant"), and minimal inline CSS.
  - A `<header>` element: MapleWave wordmark (styled text) + subtitle "Billing Adjustment Assistant".
  - A `<main>` element with a `<div id="root">` — this is the mount point required by the embed script (`rootElementID: "root"`).
  - The full wxO embed `<script>` block placed just before `</body>`.

**Relevant Context**  
- Embed script sets `rootElementID: "root"` — the `<div id="root">` in `<main>` must match exactly.  
- `orchestrationID`, `hostURL`, `agentId`, and `agentEnvironmentId` values come verbatim from the provided embed code — do not modify them.  
- No secrets are involved; the embed code is public client-side JS.

**Status** `[ ] pending`

---

### 2 — Create `website/serve.py`

**Intent**  
Give the user a one-command way to serve the site locally so the browser can load the external wxO script without file:// CORS issues.

**Expected Outcomes**  
- Running `python website/serve.py` starts a local HTTP server on port 8000 (or a configurable port) rooted at the `website/` directory.  
- A startup message prints the URL to open in the browser.  
- The script has a one-line docstring per project convention.

**Todo List**  
- [ ] Write `website/serve.py` using Python's built-in `http.server` module — no third-party packages.  
- [ ] Set the server root to the directory containing `serve.py` so paths resolve correctly regardless of where the script is invoked from.  
- [ ] Print a clear startup message, e.g. `Serving MapleWave site at http://localhost:8000 — press Ctrl+C to stop`.  
- [ ] Add a one-line module docstring.

**Relevant Context**  
- Project rule: write scripts in Python, not bash. Use the existing `venv` if imports are needed (none required here — stdlib only).  
- Port 8000 is the Python `http.server` default and is fine for local demo use.

**Status** `[ ] pending`

---

### 3 — Update `README.md` with local dev instructions

**Intent**  
Make it easy for anyone picking up the repo to find and run the website.

**Expected Outcomes**  
- `README.md` contains a short "Website" section explaining how to run `serve.py` and open the browser.

**Todo List**  
- [ ] Append (or insert) a `## Website` section to `README.md` with two steps: run the server, open the URL.

**Relevant Context**  
- Keep the addition short — two to four lines. Do not restructure the existing README content.

**Status** `[ ] pending`
