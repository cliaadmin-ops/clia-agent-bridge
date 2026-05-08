# CLIA Agent Bridge

This directory contains the scaffolding for the administrative interface that allows the CLIA Gemini Agent to interact with the website repository.

## Setup
For instructions on how to configure GitHub App authentication, see [GITHUB_APP_SETUP.md](./GITHUB_APP_SETUP.md).

## Components
1. **OAuth Layer:** Google OAuth 2.0 integration restricted to the `canadaragolake.com` domain.
2. **Git-Ops Engine:** A Python/Node.js service that translates agent intent into Git commands (branch, commit, push, merge).
3. **Voice Interface:** Integration with the OpenCode Voice Bridge protocols for STT/TTS interaction.
4. **Document Processor:** Tooling to parse uploaded PDFs and Word documents for content extraction.

## Workflow
1. User authenticates via Google.
2. User provides update (Voice/Text/File).
3. Agent processes update and creates a branch.
4. Agent pushes to `dev` and provides a preview link.
5. Upon user approval, Agent merges to `main`.

## Security
- **No Direct Prod Access:** The agent cannot push to `main` without a successful `dev` deployment and explicit user confirmation.
- **Audit Log:** Every action is logged to the repository's commit history.
