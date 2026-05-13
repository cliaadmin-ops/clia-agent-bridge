# CLIA Agent Bridge (v1.4.0-stable)

This directory contains the FastAPI service that allows the CLIA Gemini Agent to interact with the website repository via a hardened GitOps workflow.

## Setup
For instructions on how to configure GitHub App authentication, see [GITHUB_APP_SETUP.md](./GITHUB_APP_SETUP.md).

## Components
1. **OAuth Layer:** Google OAuth 2.0 integration restricted to the `canadaragolake.com` domain.
2. **Git-Ops Engine:** A Python service that translates agent intent into Git commands (branch, commit, push, merge).
3. **Path Security:** Uses `get_safe_path` (pathlib-based) to force all operations into the `public/` subdirectory, preventing path traversal and root-level write errors.
4. **Voice Interface:** Integration with the OpenCode Voice Bridge protocols for STT/TTS interaction.
5. **Document Processor:** Tooling to parse uploaded PDFs and Word documents for content extraction.

## The "Two-Gate" Workflow (v1.4.0)
1. **Gate 1: Staging (Dev)**
   - User provides update (Voice/Text/File).
   - Agent identifies target file using `site-manifest.json`.
   - Agent reads current content, generates a staging plan, and presents it to the user.
   - User clicks **"Confirm & Stage"**.
   - Agent creates a feature branch, applies changes, and merges into `dev`.
2. **Gate 2: Production (Main)**
   - User verifies changes on the [Dev Site](https://clia-dev-378290023292.us-east1.run.app).
   - User clicks **"Approve & Push to Production"**.
   - Agent merges `dev` into `main` and pushes to the live site.

## Security
- **No Direct Prod Access:** The agent cannot push to `main` without explicit user confirmation at the Production gate.
- **Path Isolation:** All file operations are restricted to the `public/` directory.
- **Audit Log:** Every action is logged to the repository's commit history with the requesting user's email.
