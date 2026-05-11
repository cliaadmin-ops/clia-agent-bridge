# Technical Specification: CLIA Agent Bridge

## System Overview
The CLIA Agent Bridge is a standalone FastAPI service that acts as an autonomous Git-Ops orchestrator for the CLIA website. It bridges the gap between natural language requests and structured repository updates.

## Core Architecture
- **Backend:** FastAPI, `google-genai` SDK (Gemini Enterprise Agent Platform).
- **Git-Ops:** `GitPython` managing a local clone of the website repository.
- **Persistence:** Firestore (Async) for chat history; LocalStorage for UI session continuity.
- **Deployment:** Cloud Run (Global Endpoint).

## The "Two-Gate" Workflow (Safety Protocol)
1.  **Staging (Gate 1):**
    - Agent identifies the target file, reads current content, and generates the update.
    - Agent pushes changes to the `dev` branch.
    - User verifies changes on the Dev URL.
2.  **Production (Gate 2):**
    - User clicks "Approve & Push to Production" in the UI.
    - Agent merges `dev` branch into `main` and pushes to production.
    - **CRITICAL:** The Agent MUST NEVER push to `main` without explicit user approval.

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
