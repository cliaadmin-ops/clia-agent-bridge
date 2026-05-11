import os
import json
import time
import requests
import PyPDF2
import docx
import google.auth
import google.auth.transport.requests
from google import genai
from google.genai import types
from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, Header, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Dict, Any, Optional, List
from git_ops import GitOps
from history import ChatHistoryManager
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="CLIA Agent Bridge")
chat_manager = ChatHistoryManager()

# Configuration (In production, these come from Secret Manager)
REPO_PATH = os.getenv("REPO_PATH", "/tmp/clia-website")
REMOTE_URL = "https://github.com/cliaadmin-ops/clia-website.git"

# Auth Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # Legacy/Fallback
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_INSTALLATION_ID = os.getenv("GITHUB_INSTALLATION_ID")
GITHUB_PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")

if GITHUB_PRIVATE_KEY:
    # Handle potential newline issues in env vars
    GITHUB_PRIVATE_KEY = GITHUB_PRIVATE_KEY.replace("\\n", "\n")

git_ops = GitOps(
    REPO_PATH, 
    REMOTE_URL, 
    app_id=GITHUB_APP_ID, 
    private_key=GITHUB_PRIVATE_KEY, 
    installation_id=GITHUB_INSTALLATION_ID,
    token=GITHUB_TOKEN
)

# Simple in-memory lock for concurrent edits (Note: This only works per-instance)
# For multi-instance Cloud Run, we'd use Firestore or GCS.
AGENT_LOCK = {"locked": False, "user": None, "timestamp": 0}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Initialize Gemini Enterprise Agent Platform Client
PROJECT_ID = "clia-web-prod"
LOCATION = "global"

# The genai.Client will pick up default credentials from the environment in Cloud Run
client = genai.Client(
    vertexai=True,
    project=PROJECT_ID,
    location=LOCATION
)
print(f"DEBUG: Gemini Enterprise Agent Platform initialized for project: {PROJECT_ID} in {LOCATION}")

def get_gemini_response(prompt: str, model_name: str = "gemini-3.1-flash-lite", contents: list = None) -> str:
    try:
        if contents:
            # contents is a list of parts or strings
            response = client.models.generate_content(
                model=model_name,
                contents=contents
            )
        else:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
        return response.text.strip()
    except Exception as e:
        error_msg = f"Agent Platform Error ({model_name}): {str(e)}"
        print(error_msg)
        return f"ERROR: {error_msg}"

def parse_document(file: UploadFile) -> bytes:
    """Returns the raw bytes of the document for Gemini 3 native processing."""
    try:
        content = file.file.read()
        file.file.seek(0)
        return content
    except Exception as e:
        print(f"Error reading document: {e}")
        return b""

async def get_iap_user(x_goog_authenticated_user_email: Optional[str] = Header(None)):
    if not x_goog_authenticated_user_email:
        # In local development, we might not have this header
        if os.getenv("ENV") == "dev":
            return "dev-user@canadaragolake.com"
        raise HTTPException(status_code=401, detail="Missing IAP authentication header")
    # IAP header format is 'accounts.google.com:user@email.com'
    return x_goog_authenticated_user_email.split(":")[-1]

class UpdateRequest(BaseModel):
    target: str  # e.g., "board", "species"
    data: Any
    message: str

@app.get("/health")
def health_check():
    """Non-blocking health check endpoint."""
    return {
        "status": "healthy",
        "repo_initialized": os.path.exists(REPO_PATH),
        "timestamp": time.time()
    }

@app.get("/hello")
async def hello_vertex():
    """Test endpoint to verify Vertex AI connectivity."""
    response = get_gemini_response("Say hello from the CLIA Agent Bridge!", "gemini-3.1-flash-lite")
    return {"response": response, "project": PROJECT_ID, "location": LOCATION}

@app.get("/")
def read_root(user: str = Depends(get_iap_user)):
    return {"status": "CLIA Agent Bridge Active", "user": user}

@app.get("/agent", response_class=HTMLResponse)
async def get_agent_ui(request: Request, user: str = Depends(get_iap_user)):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"user": user}
    )

@app.get("/agent/status/{message_id}")
async def get_status(message_id: str, user: str = Depends(get_iap_user)):
    msg = await chat_manager.get_message_by_id(message_id)
    if not msg:
        return HTMLResponse(content="<p class='text-red-500'>Error: Status not found</p>")
    
    content = msg.get("content", "")
    
    # If the content contains the verification UI or is a final answer, stop polling
    if "message-agent" in content and "hx-get" not in content:
        return HTMLResponse(content=content)
        
    # Still processing
    return HTMLResponse(content=f"""
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-yellow-50 border border-yellow-200"
         id="status-container-{message_id}"
         hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <p class="text-sm text-yellow-800" id="status-message">{content}</p>
    </div>
    """)

async def chat_worker(user: str, message: str, doc_bytes: Optional[bytes], message_id: str, history_context: str):
    global AGENT_LOCK
    try:
        # 1. Triage & Complexity ID (Gemini 3.1 Flash Lite)
        await chat_manager.update_message(message_id, "Analyzing request...")
        triage_prompt = f"""
        Analyze the user's request and return a JSON object with:
        - "intent": "READ" or "WRITE"
        - "complexity": 1-10 (1=Simple query, 5=Standard update, 10=Complex reasoning/synthesis)
        - "target": "board", "species", or "general"
        
        Recent Conversation:
        {history_context}

        User Message: {message}
        Has Document: {bool(doc_bytes)}
        """
        triage_raw = get_gemini_response(triage_prompt, "gemini-3.1-flash-lite")
        try:
            triage_json = json.loads(triage_raw.strip('`').replace('json\n', ''))
            intent = triage_json.get("intent", "READ")
            complexity = triage_json.get("complexity", 1)
        except:
            intent = "READ"
            complexity = 1

        # 2. Dynamic Model Routing
        if complexity <= 3:
            model_to_use = "gemini-3.1-flash-lite"
        elif complexity <= 7:
            model_to_use = "gemini-3-flash-preview"
        else:
            model_to_use = "gemini-3.1-pro-preview"
        
        if intent == "WRITE" or doc_bytes:
            # Acquire Lock
            AGENT_LOCK["locked"] = True
            AGENT_LOCK["user"] = user
            AGENT_LOCK["timestamp"] = time.time()

            # 0. Get Git Context
            await chat_manager.update_message(message_id, "Checking Git status...")
            git_ops.repo.remotes.origin.fetch()
            diff_dev_main = git_ops.repo.git.diff('main..origin/dev', '--stat')
            git_context = f"""
            Current Git Status:
            - Production (main) is the baseline.
            - Staging (dev) has the following pending changes not yet in production:
            {diff_dev_main if diff_dev_main else "None (dev is in sync with main)"}
            """

            await chat_manager.update_message(message_id, "Identifying target files...")
            
            # Extraction & Staging Logic
            staging_model = "gemini-3-flash-preview" if complexity <= 7 else "gemini-3.1-pro-preview"
            
            # 1. Identify the target file
            manifest_path = os.path.join(REPO_PATH, "public", "site-manifest.json")
            manifest_content = ""
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r') as f:
                    manifest_content = f.read()

            files_content = {}
            files_list = []
            for root, dirs, files in os.walk(os.path.join(REPO_PATH, "public")):
                for f in files:
                    if f.endswith((".html", ".json")):
                        rel_path = os.path.relpath(os.path.join(root, f), REPO_PATH)
                        files_list.append(rel_path)
                        # Only read snippets for HTML files to save tokens; JSON is usually small or in manifest
                        if f.endswith(".html"):
                            with open(os.path.join(root, f), 'r', encoding='utf-8') as file:
                                files_content[rel_path] = file.read(1000)
            
            identify_prompt = f"""
            You are the CLIA Website Agent.
            User Request: {message}
            
            Site Manifest:
            {manifest_content}
            
            Available Files and snippets:
            {json.dumps(files_content, indent=2)}
            
            TASK:
            1. Identify the file that needs to be modified.
            2. If the request is about "board" or "species", prioritize the JSON files in the manifest.
            3. Return ONLY the file path.
            """
            file_to_change = get_gemini_response(identify_prompt, "gemini-3.1-flash-lite").strip().strip("'").strip('"')
            
            if file_to_change not in files_list:
                # Fallback logic: if it's a known target from manifest, use that
                if "board" in message.lower():
                    file_to_change = "public/data/board.json"
                elif "species" in message.lower():
                    file_to_change = "public/data/species.json"
                else:
                    file_to_change = "public/index.html"

            await chat_manager.update_message(message_id, f"Reading {file_to_change}...")

            # 2. Read the actual content
            full_file_path = os.path.join(REPO_PATH, file_to_change)
            current_content = ""
            if os.path.exists(full_file_path):
                with open(full_file_path, 'r', encoding='utf-8') as f:
                    current_content = f.read()

            await chat_manager.update_message(message_id, "Generating staging plan...")

            # 3. Generate the update
            staging_prompt = f"""
            You are the CLIA Website Agent. Your primary directive is to follow user instructions EXACTLY.
            
            {git_context}
            
            USER REQUEST: {message}
            FILE TO CHANGE: {file_to_change}
            
            CURRENT FILE CONTENT:
            ---
            {current_content}
            ---
            
            TASK:
            1. Modify the file content to satisfy the user request.
            2. DO NOT make any changes that were not explicitly requested. No "creative improvements," no extra banners, no layout changes unless asked.
            3. If the file is JSON, ensure the output is valid JSON.
            4. Provide the EXACT new content for the entire file.
            5. Provide a short summary of the change.
            
            Return a JSON object with:
            {{
                "new_content": "full content of the file",
                "summary": "Short summary of changes"
            }}
            """
            
            staging_raw = get_gemini_response(staging_prompt, staging_model)
            try:
                json_str = staging_raw
                if "```json" in json_str:
                    json_str = json_str.split("```json")[1].split("```")[0]
                elif "```" in json_str:
                    json_str = json_str.split("```")[1].split("```")[0]
                
                staging_json = json.loads(json_str.strip())
                new_content = staging_json.get("new_content")
                extraction_result = staging_json.get("summary", "Update staged.")
                
                if new_content:
                    # GATE 1: STOP HERE AND ASK FOR CONFIRMATION
                    # We store the plan in the message content as a hidden JSON or just the UI
                    # For now, we'll present the "Confirm Selection" UI.
                    
                    # We need to store the new_content somewhere. 
                    # Let's use a temporary file or just keep it in the Firestore message for now.
                    # Actually, the simplest way is to just proceed to staging but with a "Confirm" step.
                    # But you asked to stop BEFORE the push.
                    
                    # To stop before the push, we need a new endpoint to handle the confirmation.
                    # For now, let's just implement the UI that asks for confirmation.
                    
                    confirm_html = f"""
                    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-blue-50 border border-blue-200">
                        <h3 class="font-bold text-blue-800">Step 1: Verify Selection</h3>
                        <p class="text-sm mb-2">I've identified the file to change and prepared the update:</p>
                        <code class="block bg-gray-100 p-2 rounded text-xs font-mono mb-2">{file_to_change}</code>
                        <p class="text-xs italic mb-4">Summary: {extraction_result}</p>
                        
                        <div class="flex space-x-2">
                            <form hx-post="/agent/confirm-stage" 
                                  hx-target="closest .message-agent"
                                  hx-swap="outerHTML"
                                  class="flex-1">
                                <input type="hidden" name="message_id" value="{message_id}">
                                <input type="hidden" name="file" value="{file_to_change}">
                                <input type="hidden" name="summary" value="{extraction_result}">
                                <input type="hidden" name="content" value="{new_content.replace('"', '&quot;')}">
                                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white text-xs font-bold py-2 px-3 rounded">
                                    Confirm & Stage to Dev
                                </button>
                            </form>
                            <button hx-post="/agent/discard"
                                    hx-target="closest .message-agent"
                                    hx-swap="outerHTML"
                                    class="bg-gray-400 hover:bg-gray-500 text-white text-xs font-bold py-2 px-3 rounded">
                                Discard
                            </button>
                        </div>
                    </div>
                    """
                    await chat_manager.update_message(message_id, confirm_html)
                else:
                    await chat_manager.update_message(message_id, "<p class='text-red-500'>Error: Failed to generate content.</p>")
                    AGENT_LOCK["locked"] = False

            except Exception as e:
                await chat_manager.update_message(message_id, f"<p class='text-red-500'>Error parsing staging plan: {str(e)}</p>")
                AGENT_LOCK["locked"] = False

        else:
            # READ Logic
            await chat_manager.update_message(message_id, "Searching website data...")
            manifest_path = os.path.join(REPO_PATH, "public", "site-manifest.json")
            context = "Current Website Data:\n"
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r') as f:
                    manifest = json.load(f)
                for target, info in manifest["editable_content"].items():
                    file_path = os.path.join(REPO_PATH, "public", info["path"].lstrip("/"))
                    if os.path.exists(file_path):
                        with open(file_path, 'r') as f:
                            data = json.load(f)
                            context += f"\n{target.upper()}:\n{json.dumps(data, indent=2)}\n"
            
            query_prompt = f"""
            You are the CLIA Website Agent. 
            Current Website Data:
            {context}
            
            Recent Conversation:
            {history_context}
            
            User Question: {message}
            
            INSTRUCTIONS:
            - Provide a concise, helpful answer.
            - Do NOT give technical Git instructions.
            - Maintain a professional, supportive tone.
            """
            answer = get_gemini_response(query_prompt, model_to_use)
            
            final_html = f"""
            <div class="message-agent p-3 rounded-lg max-w-[80%]">
                <div class="text-[10px] text-gray-400 mb-1 uppercase font-bold">{model_to_use} (C{complexity})</div>
                {answer}
            </div>
            """
            await chat_manager.update_message(message_id, final_html)

    finally:
        # Release Lock if it was acquired by THIS worker
        if AGENT_LOCK["locked"] and AGENT_LOCK["user"] == user:
            AGENT_LOCK["locked"] = False
            print(f"DEBUG: Released lock for {user}")

@app.post("/agent/chat")
async def chat_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    message: str = Form(...),
    file: Optional[UploadFile] = File(None),
    user: str = Depends(get_iap_user)
):
    global AGENT_LOCK
    # Check lock (expire after 10 mins)
    if AGENT_LOCK["locked"] and AGENT_LOCK["user"] != user and (time.time() - AGENT_LOCK["timestamp"]) < 600:
        return HTMLResponse(content=f"""
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-yellow-50 border border-yellow-200">
            <p class="text-sm text-yellow-800"><b>Agent Busy:</b> {AGENT_LOCK['user']} is currently staging an update. Please try again in a few minutes.</p>
        </div>
        """)

    # Retrieve persistent chat history
    history = await chat_manager.get_recent_messages(user, limit=10)
    history_context = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in history])

    doc_bytes = None
    if file and file.filename:
        doc_bytes = parse_document(file)
    
    # Save user message to history
    await chat_manager.save_message(user, "user", message)

    # Create placeholder message in history
    message_id = await chat_manager.save_message(user, "assistant", "Processing...")

    # Trigger background worker
    background_tasks.add_task(chat_worker, user, message, doc_bytes, message_id, history_context)

    # Return immediate HTMX for polling
    return HTMLResponse(content=f"""
    <div class="message-user p-3 rounded-lg max-w-[80%]">
        {message}
        {f"<br><i class='text-xs'>(File attached: {getattr(file, 'filename', getattr(file, 'name', 'Unknown'))})</i>" if file else ""}
    </div>
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-yellow-50 border border-yellow-200" 
         id="status-container-{message_id}"
         hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <p class="text-sm text-yellow-800" id="status-message">Processing...</p>
    </div>
    """)

@app.get("/agent/deploy-status")
async def get_deploy_status(branch: str, user: str = Depends(get_iap_user)):
    """
    Checks the Cloud Run service status directly via the Google Cloud API.
    This avoids GitHub 403 issues and shows the actual deployment state.
    """
    try:
        # 1. Determine the service name based on the branch
        service_name = "clia-dev" if branch == "dev" else "clia-website"
        project_id = "clia-web-prod"
        region = "us-east1"
        
        # 2. Get Google Auth Token
        credentials, _ = google.auth.default()
        auth_request = google.auth.transport.requests.Request()
        credentials.refresh(auth_request)
        token = credentials.token
        
        # 3. Query Cloud Run Admin API
        url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_id}/services/{service_name}"
        headers = {"Authorization": f"Bearer {token}"}
        
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            return HTMLResponse(content=f"<span class='status-pill bg-gray-100 text-gray-400'>Status Error ({resp.status_code})</span>")
            
        data = resp.json()
        
        # 4. Parse Knative Service Status
        # We look for the 'Ready' condition
        conditions = data.get("status", {}).get("conditions", [])
        ready_condition = next((c for c in conditions if c["type"] == "Ready"), None)
        
        if not ready_condition:
            return HTMLResponse(content="<span class='status-pill bg-yellow-100 text-yellow-700 animate-pulse'>● Initializing</span>")
            
        status = ready_condition.get("status") # "True", "False", or "Unknown"
        reason = ready_condition.get("reason", "")
        
        if status == "True":
            return HTMLResponse(content="<span class='status-pill bg-green-100 text-green-700'>● Live</span>")
        elif status == "False":
            return HTMLResponse(content=f"<span class='status-pill bg-red-100 text-red-700' title='{reason}'>● Failed</span>")
        else:
            # status == "Unknown" usually means a deployment is in progress
            return HTMLResponse(content="<span class='status-pill bg-yellow-100 text-yellow-700 animate-pulse'>● Deploying...</span>")
            
    except Exception as e:
        print(f"DEBUG: Cloud Run status error: {e}")
        return HTMLResponse(content="<span class='status-pill bg-gray-100 text-gray-400'>Status Error</span>")


class ConfirmStageRequest(BaseModel):
    message_id: str
    file: str
    summary: str
    content: str

@app.post("/agent/confirm-stage")
async def confirm_stage(
    message_id: str = Form(...),
    file: str = Form(...),
    summary: str = Form(...),
    content: str = Form(...),
    user: str = Depends(get_iap_user)
):
    try:
        # 1. Perform Git-Ops Staging
        git_ops.sync_main()
        timestamp = int(time.time())
        branch_name = f"agent-update-{timestamp}"
        git_ops.create_content_branch(branch_name)
        
        full_file_path = os.path.join(REPO_PATH, file)
        os.makedirs(os.path.dirname(full_file_path), exist_ok=True)
        
        with open(full_file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        git_ops.commit_and_push(f"Agent Update: {summary} (Requested by {user})")
        
        # Final Verification UI
        final_html = f"""
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-blue-50 border border-blue-200">
            <div class="flex justify-between items-start mb-2">
                <h3 class="font-bold text-blue-800">Staged on Dev Site</h3>
            </div>
            <p class="text-sm mb-4">I've applied your changes to the <b>Dev Site</b> for verification.</p>
            
            <div class="bg-white p-3 rounded border border-gray-300 mb-4 text-xs font-mono whitespace-pre-wrap">
<b>Summary of Changes:</b>
{summary}
            </div>

            <div class="flex flex-col space-y-3">
                    <div class="flex items-center space-x-2">
                        <a href="{git_ops.get_dev_url()}" target="_blank" class="flex-1 bg-blue-600 hover:bg-blue-700 text-white text-center text-xs font-bold py-2 px-3 rounded no-underline">
                            Step 1: Verify on Dev Site
                        </a>
                        <div id="dev-deploy-status" 
                             hx-get="/agent/deploy-status?branch=dev" 
                             hx-trigger="load, every 10s"
                             class="status-pill bg-gray-100 text-gray-500">
                            Checking...
                        </div>
                    </div>
                
                <div class="border-t border-blue-200 pt-3">
                    <p class="text-[10px] text-blue-600 mb-2 font-bold uppercase">Step 2: Final Action</p>
                    <div class="flex space-x-2">
                        <button hx-post="/agent/approve?branch={branch_name}" 
                                hx-target="closest .message-agent" 
                                hx-swap="outerHTML"
                                class="flex-1 bg-green-600 hover:bg-green-700 text-white text-xs font-bold py-1 px-3 rounded">
                            Approve & Push to Production
                        </button>
                        <button hx-post="/agent/discard"
                                hx-target="closest .message-agent"
                                hx-swap="outerHTML"
                                class="bg-gray-400 hover:bg-gray-500 text-white text-xs font-bold py-1 px-3 rounded">
                            Discard
                        </button>
                    </div>
                </div>
            </div>
        </div>
        """
        await chat_manager.update_message(message_id, final_html)
        return HTMLResponse(content=final_html)
    except Exception as e:
        return HTMLResponse(content=f"<div class='text-red-600'>Error: {str(e)}</div>")

@app.post("/agent/update")
async def update_content(request: UpdateRequest, user: str = Depends(get_iap_user)):
    try:
        # Log the user making the change
        print(f"User {user} is updating {request.target}")
        
        # 1. Sync and Branch
        git_ops.sync_main()
        branch_name = git_ops.create_content_branch(request.target)

        # 2. Update File
        # Map target to path using site-manifest.json logic
        manifest_path = os.path.join(REPO_PATH, "public", "site-manifest.json")
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        if request.target not in manifest["editable_content"]:
            raise HTTPException(status_code=400, detail=f"Invalid target: {request.target}")
        
        relative_path = manifest["editable_content"][request.target]["path"]
        # Remove leading slash for os.path.join
        file_path = os.path.join(REPO_PATH, "public", relative_path.lstrip("/"))
        
        with open(file_path, 'w') as f:
            json.dump(request.data, f, indent=2)

        # 3. Commit and Push
        git_ops.commit_and_push(request.message)

        return {
            "status": "success",
            "branch": branch_name,
            "dev_url": git_ops.get_dev_url(),
            "message": f"Changes staged on branch {branch_name}. Please review at the Dev URL."
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/agent/approve")
async def approve_update(branch: str, user: str = Depends(get_iap_user)):
    global AGENT_LOCK
    try:
        print(f"User {user} is approving branch {branch}")
        git_ops.merge_to_main(branch)
        
        # Release Lock
        AGENT_LOCK["locked"] = False
        
        return HTMLResponse(content=f"""
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-green-50 border border-green-200">
            <h3 class="font-bold text-green-800 mb-1">Success!</h3>
            <p class="text-sm text-green-700">Changes from branch <b>{branch}</b> have been merged to <b>main</b> and published.</p>
            <div class="mt-2 flex items-center space-x-2">
                <div id="prod-deploy-status" 
                     hx-get="/agent/deploy-status?branch=main" 
                     hx-trigger="load, every 10s"
                     class="status-pill bg-white border border-green-200 text-green-600">
                    Checking Prod...
                </div>
                <button hx-post="/agent/revert" hx-target="closest .message-agent" class="text-[10px] text-red-600 underline">Undo (Revert Main)</button>
            </div>
        </div>
        """)
    except Exception as e:
        return HTMLResponse(content=f"<div class='text-red-600'>Error: {str(e)}</div>")

@app.post("/agent/discard")
async def discard_update(user: str = Depends(get_iap_user)):
    global AGENT_LOCK
    try:
        # 1. Reset Dev branch to Main
        success = git_ops.discard_dev_changes()
        
        # 2. Release Lock
        AGENT_LOCK["locked"] = False
        
        if success:
            return HTMLResponse(content="""
            <div class='message-agent p-3 rounded-lg max-w-[80%] bg-gray-50 border border-gray-200'>
                <p class='text-xs italic text-gray-600'>Update discarded. The changes have been surgically reverted on the <b>Dev Site</b>.</p>
            </div>
            """)
        else:
            return HTMLResponse(content="""
            <div class='message-agent p-3 rounded-lg max-w-[80%] bg-red-50 border border-red-200'>
                <p class='text-xs text-red-600'>Agent unlocked, but failed to reset Dev branch. Please check logs.</p>
            </div>
            """)
    except Exception as e:
        AGENT_LOCK["locked"] = False
        return HTMLResponse(content=f"<div class='text-red-600 text-xs'>Error: {str(e)}</div>")

@app.post("/agent/revert")
async def revert_update(user: str = Depends(get_iap_user)):
    try:
        new_sha = git_ops.revert_main()
        return HTMLResponse(content=f"""
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-orange-50 border border-orange-200">
            <p class="text-sm text-orange-800"><b>Reverted:</b> The last change to the live site has been undone.</p>
            <p class="text-[10px] text-gray-500">New HEAD: {new_sha[:7]}</p>
        </div>
        """)
    except Exception as e:
        return HTMLResponse(content=f"<div class='text-red-600'>Revert failed: {str(e)}</div>")

@app.post("/agent/clear")
async def clear_chat_history(user: str = Depends(get_iap_user)):
    await chat_manager.clear_history(user)
    return {"status": "success", "message": "Chat history cleared."}
