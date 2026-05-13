# Spec Sheet: Deployment Indicator Lights (v1.0-Draft)

## 1. Objective
Provide real-time, high-fidelity visual feedback to the user during the "Two-Gate" deployment process. The system must distinguish between "Serving Traffic (Old Version)" and "Actively Deploying (New Version)" to prevent premature verification.

## 2. Status States & Visuals
| State | Indicator | Logic Trigger | UX Behavior |
| :--- | :--- | :--- | :--- |
| **Idle / Stable** | Static Green | `Ready == True` AND `ObservedGen == MetaGen` | Standard "Live" pill. |
| **Triggered** | Pulsing Blue | Git Push successful, but Cloud Run hasn't registered new Gen yet. | "Preparing Deployment..." |
| **Deploying** | Pulsing Yellow | `ObservedGen < MetaGen` OR `RevisionReady == False` | "Deploying New Revision..." |
| **Traffic Shifting** | Pulsing Orange | New Revision Ready, but `TrafficSplit` for latest < 100%. | "Activating..." |
| **Failed** | Static Red | `Ready == False` OR `RevisionError` detected. | "Deployment Failed (Click for Logs)" |

## 3. Technical Challenges
- **The "Ready" Illusion:** Cloud Run Services often report `Ready: True` because the *previous* revision is still healthy.
- **Polling Latency:** HTMX polling (every 10s) might miss the transition window between "Triggered" and "Deploying".
- **Version Awareness:** The UI doesn't currently know *which* version it is looking at, only that the service is "Ready".

## 4. Technical Implementation (Research Findings)

### A. Multi-Layered Cloud Run Detection
The current `observedGeneration` check is a good start but insufficient. A robust check must follow this sequence:
1.  **Spec Sync:** `status.observedGeneration == metadata.generation`. (Is the controller aware of the change?)
2.  **Revision Health:** `status.latestCreatedRevisionName == status.latestReadyRevisionName`. (Is the new container running and healthy?)
3.  **Traffic Shift:** `status.traffic` entry for `latestReadyRevisionName` has `percent: 100`. (Is the new version actually receiving all traffic?)

### B. The "Edge-Aware" Verification (Version.json)
To solve the "Stale Cache" problem where Cloud Run is finished but the CDN/Browser still shows the old site:
1.  **Build Step:** Modify `cloudbuild.yaml` (or the deployment script) to generate a `public/version.json` containing the Git SHA.
2.  **External Probe:** The Bridge performs an HTTP GET to the site's `/version.json` and compares the SHA.
3.  **State:** The indicator remains in "Activating (Propagating)..." until the external SHA matches the expected SHA.

### C. Proposed Logic Refactor
```python
def get_deploy_status(service_data, expected_sha=None):
    status = service_data.get("status", {})
    traffic = status.get("traffic", [])
    
    # 1. Spec Check
    if status.get("observedGeneration", 0) < service_data.get("metadata", {}).get("generation", 0):
        return "Deploying (Reconciling Spec...)"
    
    # 2. Revision Check
    if status.get("latestCreatedRevisionName") != status.get("latestReadyRevisionName"):
        return "Deploying (Initializing Container...)"
    
    # 3. Traffic Check
    latest_traffic = next((t for t in traffic if t.get("revisionName") == status.get("latestReadyRevisionName")), None)
    if not latest_traffic or latest_traffic.get("percent", 0) < 100:
        return "Deploying (Shifting Traffic...)"
    
    # 4. External Version Check (Optional but Recommended)
    if expected_sha:
        # Perform HTTP GET to /version.json
        # Return "Activating (Propagating)..." if mismatch
        pass

    return "Live"
```

## 5. Next Steps for Implementation
1.  **Update `clia-website` Build Process:** Add a step to generate `version.json`.
2.  **Update `clia-agent-bridge` API:** 
    - Enhance `/agent/deploy-status` to parse the full Service JSON.
    - Add an optional `sha` parameter to the status endpoint.
3.  **UI Updates:** Map the new sub-states to the pulsing indicator colors defined in Section 2.
