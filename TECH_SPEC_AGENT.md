# Technical Specification: CLIA Agent Bridge

## System Overview
The CLIA Agent Bridge is a standalone FastAPI service that acts as an autonomous Git-Ops orchestrator for the CLIA website. It bridges the gap between natural language requests and structured repository updates.

## Core Architecture
- **Backend:** FastAPI, `google-genai` SDK (Gemini Enterprise Agent Platform).
- **Git-Ops:** `GitPython` managing a local clone of the website repository.
- **Persistence:** Firestore (Async) for chat history; LocalStorage for UI session continuity.
- **Deployment:** Cloud Run (Global Endpoint).

## The "Two-Gate" Workflow (Stable v1.4.0)
1.  **Staging (Gate 1):**
    - User requests a change (e.g., "Update footer to v1.3").
    - Agent identifies target, reads current content, and generates a staging plan.
    - User reviews the plan in the UI and clicks **"Confirm & Stage"**.
    - Agent creates a feature branch, applies changes, and merges into the `dev` branch.
2.  **Production (Gate 2):**
    - User verifies the changes on the **Dev Site URL**.
    - User returns to the UI and clicks **"Approve & Push to Production"**.
    - Agent merges the `dev` state into `main` and pushes to the live site.
    - **CRITICAL:** The Agent MUST NEVER push to `main` without explicit user approval at this gate.

## Content-Aware Staging Protocol
When a user requests a change:
1.  **Identify:** Agent scans the `public/` directory and reads file snippets to identify the correct file.
2.  **Read:** Agent reads the *full* current content of the target file.
3.  **Edit:** Agent generates the *full* new content of the file.
4.  **Commit:** Agent commits with the message: `Agent Update: [Summary] (Requested by [user_email])`.

## Cross-Repo Documentation Sync
- **Website Repo:** Content manifest, site structure.
- **Bridge Repo:** Agent logic, Git-Ops workflow, API endpoints.
- **Protocol:** Any change to the website structure (e.g., new editable JSON file) must be reflected in `site-manifest.json` AND documented in the Bridge's `docs/` folder.
