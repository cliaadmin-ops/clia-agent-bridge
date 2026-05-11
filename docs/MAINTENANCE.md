# CLIA Website Maintenance Guide

## Architecture Overview
- **Repository:** `cliaadmin-ops/clia-website`
- **Agent Bridge:** `cliaadmin-ops/clia-agent-bridge`
- **Hosting:** Google Cloud Run (Continuous Deployment)
- **Database:** Google Firestore (Chat History)

## The "Two-Gate" Workflow
1. **Request:** User sends a request via the Agent Switchboard.
2. **Gate 1 (Verification):** Agent identifies the file and prepares a staging plan. User must click **Confirm** to proceed.
3. **Staging:** Agent merges changes into the `dev` branch.
4. **Verification:** User reviews the changes on the [Dev Site](https://dev.canadaragolake.com).
5. **Gate 2 (Approval):** User clicks **Approve & Publish**.
6. **Production:** The `dev` branch is merged into `main`, triggering a production deployment.

## Key Files
- `public/index.html`: Main landing page.
- `public/data/board.json`: Structured data for the board members.
- `public/data/species.json`: Structured data for invasive species.
- `public/site-manifest.json`: Registry for mapping natural language targets to file paths.

## Maintenance Tasks
- **Reverting Production:** Use the **Undo** button in the Agent UI or run `git revert HEAD` on the `main` branch.
- **Updating Manifest:** If new editable sections are added, update `public/site-manifest.json` so the Agent can find them.
- **Monitoring Logs:** Check Google Cloud Run logs for the `clia-agent-bridge` service for troubleshooting.

## Versioning
- Current Stable: `v1.3.0-stable`
