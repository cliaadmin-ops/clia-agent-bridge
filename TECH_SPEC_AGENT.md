# Tech Spec: CLIA Agent Bridge (v1.0)

## Overview
The Agent Bridge is a secure middleware service that allows the CLIA Gemini Agent to perform content updates on the website repository using a Git-Ops workflow.

## 1. Architecture
- **Runtime:** Python 3.11+ (FastAPI)
- **Deployment:** Cloud Run (in the `eminent-goods` project)
- **Authentication:** Google OAuth 2.0 (Restricted to `@canadaragolake.com`)
- **Storage:** Ephemeral (Clones repo to `/tmp` or uses a persistent volume if needed)

## 2. API Endpoints

### `POST /auth/login`
- Handles Google OAuth callback.
- Verifies domain and issues a session JWT.

### `POST /agent/update`
- **Payload:**
  ```json
  {
    "action": "update_data",
    "target": "board",
    "data": { ... },
    "message": "Update board member"
  }
  ```
- **Logic:**
  1. Validates session.
  2. Pulls latest `main`.
  3. Creates `content-update-[ts]` branch.
  4. Updates `public/data/board.json`.
  5. Commits and pushes to `origin`.
  6. Returns the Dev URL for review.

### `POST /agent/approve`
- **Payload:** `{"branch": "content-update-[ts]"}`
- **Logic:** Merges branch to `main` and pushes.

### `POST /agent/parse-doc`
- **Payload:** Multipart File (PDF/Docx)
- **Logic:** Extracts text and returns a structured JSON summary for the agent to process.

## 3. Security
- **Authentication:** Google Identity-Aware Proxy (IAP).
- **Access Control:** Restricted to the `sitemanagers@canadaragolake.com` Google Group via the `IAP-secured Web App User` role.
- **Identity Verification:** The application will verify the `X-Goog-Authenticated-User-Email` header to log user actions.
- **GitHub Token:** Stored in GCP Secret Manager.
- **Branch Protection:** The bridge is the only entity allowed to push to `main` (besides admins).
- **Audit Log:** All actions logged to a BigQuery table or simple log file.

---

## Next Steps
1. Initialize `requirements.txt`.
2. Implement `git_ops.py` using `GitPython`.
3. Scaffold `main.py` with FastAPI.
