import os
import json
import time
import base64
import requests
import PyPDF2
import docx
import google.auth
import google.auth.transport.requests
from pathlib import Path
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

# Configuration
REPO_PATH = os.getenv("REPO_PATH", "/tmp/clia-website")
REMOTE_URL = "https://github.com/cliaadmin-ops/clia-website.git"

# Auth Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_INSTALLATION_ID = os.getenv("GITHUB_INSTALLATION_ID")
GITHUB_PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")

if GITHUB_PRIVATE_KEY:
    GITHUB_PRIVATE_KEY = GITHUB_PRIVATE_KEY.replace("\\n", "\n")

git_ops = GitOps(
    REPO_PATH, 
    REMOTE_URL, 
    app_id=GITHUB_APP_ID, 
    private_key=GITHUB_PRIVATE_KEY, 
    installation_id=GITHUB_INSTALLATION_ID,
    token=GITHUB_TOKEN
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Initialize Gemini Client
PROJECT_ID = "clia-web-prod"
LOCATION = "global"
client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

def get_safe_path(repo_path: str, file_path: str) -> Path:
    """
    Unifies path resolution across all gates.
    Ensures files are always read/written within the 'public' directory.
    Handles leading slashes and 'public/' prefixes gracefully.
    """
    base = Path(repo_path).resolve()
    # Remove leading slash and redundant 'public/' prefix
    clean_path = file_path.lstrip('/')
    if clean_path.startswith('public/'):
        clean_path = clean_path[7:]
    
    # Force into public/ subdirectory
    final_path = (base / "public" / clean_path).resolve()
    
    # Security check: Ensure we haven't escaped the repo
    if not str(final_path).startswith(str(base)):
        raise ValueError(f"Path escape detected: {file_path}")
        
    return final_path

def get_gemini_response(prompt: str, model_name: str = "gemini-3-flash-preview", contents: list = None) -> str:
    try:
        if contents:
            response = client.models.generate_content(model=model_name, contents=contents)
        else:
            response = client.models.generate_content(model=model_name, contents=prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini Error: {e}")
        return f"ERROR: {str(e)}"

def parse_document(file: UploadFile) -> bytes:
    try:
        content = file.file.read()
        file.file.seek(0)
        return content
    except Exception as e:
        print(f"Error reading document: {e}")
        return b""

async def get_iap_user(x_goog_authenticated_user_email: Optional[str] = Header(None)):
    if not x_goog_authenticated_user_email:
        if os.getenv("ENV") == "dev":
            return "dev-user@canadaragolake.com"
        raise HTTPException(status_code=401, detail="Missing IAP authentication header")
    return x_goog_authenticated_user_email.split(":")[-1]

class UpdateRequest(BaseModel):
    target: str
    data: Any
    message: str

@app.get("/health")
def health_check():
    return {"status": "healthy", "repo_initialized": os.path.exists(REPO_PATH), "timestamp": time.time()}

@app.get("/")
def read_root(user: str = Depends(get_iap_user)):
    return {"status": "CLIA Agent Bridge Active", "user": user}

@app.get("/agent", response_class=HTMLResponse)
async def get_agent_ui(request: Request, user: str = Depends(get_iap_user)):
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})

@app.get("/agent/status/{message_id}")
async def get_status(message_id: str, user: str = Depends(get_iap_user)):
    msg = await chat_manager.get_message_by_id(message_id)
    if not msg:
        return HTMLResponse(content="<p class='text-red-500'>Error: Status not found</p>")
    content = msg.get("content", "")
    if "message-agent" in content and "hx-get" not in content:
        return HTMLResponse(content=content)
    return HTMLResponse(content=f"""
    <div class="flex justify-start" id="status-container-{message_id}" hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <div class="max-w-[85%] glass p-4 rounded-2xl rounded-tl-none flex items-center space-x-3">
            <div class="animate-spin h-4 w-4 border-2 border-teal border-t-transparent rounded-full"></div>
            <p class="text-xs font-bold text-navy/50 uppercase tracking-widest">{content}</p>
        </div>
    </div>
    """)

async def rollback_prod_worker(user: str, message_id: str):
    try:
        # Perform rollback on both branches
        new_sha = git_ops.revert_main()
        
        await chat_manager.update_message(message_id, f"Production Rollback successful (New HEAD: {new_sha[:7]}). Analyzing...")

        # Get history for context
        history = await chat_manager.get_recent_messages(user, limit=5)
        history_context = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in history])

        # Generate follow-up question
        prompt = f"""
        You are the CLIA Content Steward. 
        The user just rolled back your last change from the PRODUCTION site.
        
        Context of previous interaction:
        {history_context}
        
        Acknowledge the production rollback and ask 1-2 specific questions to understand what was wrong with the change 
        so you can fix it. Be professional and slightly more formal since this was a production event.
        """
        
        question = get_gemini_response(prompt, "gemini-3.1-flash-lite")
        
        final_html = f"""
        <div class="message-agent p-4 glass border-l-4 border-sunset rounded-xl max-w-[85%]">
            <div class="flex items-center space-x-2 mb-2 text-sunset">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
                <p class="text-sm font-bold uppercase tracking-wider">Production Reverted</p>
            </div>
            <div class="markdown-content text-navy text-sm">
                {question}
            </div>
            <span class="text-[10px] text-navy/50 mt-2 block">System • Just now</span>
        </div>
        """
        await chat_manager.update_message(message_id, final_html)
    except Exception as e:
        print(f"Prod Rollback Worker Error: {e}")
        await chat_manager.update_message(message_id, f"System Error during production rollback: {e}")
    finally:
        await chat_manager.release_lock(user)

async def rollback_dev_worker(user: str, message_id: str):
    try:
        # Perform rollback
        success = git_ops.revert_dev()
        if not success:
            await chat_manager.update_message(message_id, "Error: Rollback failed.")
            await chat_manager.release_lock(user)
            return

        await chat_manager.update_message(message_id, "Rollback successful. Analyzing what went wrong...")

        # Get history for context
        history = await chat_manager.get_recent_messages(user, limit=5)
        history_context = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in history])

        # Generate follow-up question
        prompt = f"""
        You are the CLIA Content Steward. 
        The user just rolled back your last change to the dev site.
        
        Context of previous interaction:
        {history_context}
        
        Acknowledge the rollback and ask 1-2 specific questions to understand what was wrong with the change 
        so you can fix it. Be helpful and professional.
        """
        
        question = get_gemini_response(prompt, "gemini-3.1-flash-lite")
        
        final_html = f"""
        <div class="message-agent p-4 glass border-l-4 border-sunset rounded-xl max-w-[85%]">
            <div class="flex items-center space-x-2 mb-2 text-sunset">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
                <p class="text-sm font-bold uppercase tracking-wider">Rollback Complete</p>
            </div>
            <div class="markdown-content text-navy text-sm">
                {question}
            </div>
            <span class="text-[10px] text-navy/50 mt-2 block">System • Just now</span>
        </div>
        """
        await chat_manager.update_message(message_id, final_html)
    except Exception as e:
        print(f"Rollback Worker Error: {e}")
        await chat_manager.update_message(message_id, f"System Error during rollback: {e}")
    finally:
        await chat_manager.release_lock(user)

async def chat_worker(user: str, message: str, doc_bytes: Optional[bytes], message_id: str, history_context: str):
    try:
        # Acquire Persistent Lock
        if not await chat_manager.acquire_lock(user):
            await chat_manager.update_message(message_id, "Agent Busy: Another task is in progress.")
            return

        await chat_manager.update_message(message_id, "Syncing repository...")
        git_ops.sync_main()
        
        # Check for pending changes on dev
        diff_stat = git_ops.repo.git.diff('main..origin/dev', '--stat')
        git_context = f"Baseline: main. Pending on dev: {diff_stat if diff_stat else 'None'}"

        await chat_manager.update_message(message_id, "Analyzing request...")
        
        # Load manifest for triage context
        manifest_path = os.path.join(REPO_PATH, "public", "site-manifest.json")
        manifest_content = "{}"
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as f:
                manifest_content = f.read()

        triage_prompt = f"""
        You are the CLIA Content Steward. 
        Analyze the user's intent and the site manifest to determine if this is a READ (question) or WRITE (modification) request.
        
        Git Context: {git_context}
        Site Manifest: {manifest_content}
        User Message: {message}
        
        Rules:
        1. If the user asks to change, update, increment, fix, or modify ANY content (including version numbers, text, or data), intent MUST be 'WRITE'.
        2. If the user asks a question about the site or board, intent is 'READ'.
        3. CRITICAL: If intent is 'WRITE' AND Git Context shows 'Pending on dev' is NOT 'None', you must still classify as 'WRITE' but set 'target_file' to 'LOCKED'.
        4. Respond ONLY with a JSON object: {{"intent": "READ"|"WRITE", "complexity": 1-10, "target_file": "path/to/file"}}
        """
        
        triage_raw = get_gemini_response(triage_prompt, "gemini-3.1-flash-lite")
        try:
            json_str = triage_raw.strip('`').replace('json\n', '')
            triage_json = json.loads(json_str)
            intent = triage_json.get("intent", "READ")
            complexity = triage_json.get("complexity", 1)
            file_to_change = triage_json.get("target_file")
        except:
            intent = "READ"
            complexity = 1
            file_to_change = None

        model_to_use = "gemini-3-flash-preview" if complexity <= 7 else "gemini-3.1-pro-preview"
        
        if intent == "WRITE" or doc_bytes:
            try:
                if file_to_change == "LOCKED":
                    lock_html = f"""
                    <div class="message-agent p-4 glass border-l-4 border-sunset rounded-xl max-w-[85%]">
                        <div class="flex items-center space-x-2 mb-2 text-sunset">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"></path></svg>
                            <p class="text-sm font-bold uppercase tracking-wider">Workflow Locked</p>
                        </div>
                        <div class="markdown-content text-navy text-sm mb-4">
                            I see there are already pending changes on the **dev** branch that haven't been pushed to production yet. 
                            
                            To prevent conflicts and ensure a stable deployment, I can only handle one set of changes at a time. You must either:
                            1. **Approve & Push** the current changes to production.
                            2. **Undo Last** to clear the dev branch and start fresh.
                        </div>
                        <div class="flex flex-col space-y-2">
                            <a href="{git_ops.get_dev_url()}" target="_blank" class="w-full bg-teal hover:bg-teal-700 text-white text-center text-xs font-bold py-2 px-4 rounded-lg transition-colors shadow-md no-underline">Review Pending Changes</a>
                        </div>
                    </div>
                    """
                    await chat_manager.update_message(message_id, lock_html)
                    await chat_manager.release_lock(user)
                    return

                await chat_manager.update_message(message_id, "Preparing staging plan...")

                # 1. Identify/Verify the target file
                if not file_to_change:
                    # Fallback to walk if triage didn't pick it up
                    files_list = []
                    for root, dirs, files in os.walk(os.path.join(REPO_PATH, "public")):
                        for f in files:
                            if f.endswith((".html", ".json")):
                                files_list.append(os.path.relpath(os.path.join(root, f), REPO_PATH))
                    file_to_change = "public/index.html" # Default
                
                full_path = get_safe_path(REPO_PATH, file_to_change)
                current_content = ""
                if full_path.exists():
                    current_content = full_path.read_text(encoding='utf-8')

                staging_prompt = f"""
                You are the CLIA Content Steward. 
                Modify {file_to_change} based on the request.
                
                Context: {git_context}
                Request: {message}
                Current Content: 
                {current_content}
                
                Respond ONLY with a JSON object:
                {{
                    "new_content": "FULL file content here",
                    "summary": "Brief explanation of change"
                }}
                """
                
                # Build multi-modal contents if doc_bytes is present
                if doc_bytes:
                    print(f"DEBUG: Sending multi-modal request to Gemini ({len(doc_bytes)} bytes)")
                    # Determine mime type (basic check)
                    mime_type = "image/jpeg"
                    if message.lower().endswith(".png") or "png" in message.lower():
                        mime_type = "image/png"
                    
                    contents = [
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(text=staging_prompt),
                                types.Part.from_bytes(data=doc_bytes, mime_type=mime_type)
                            ]
                        )
                    ]
                    staging_raw = get_gemini_response(None, "gemini-3-flash-preview", contents=contents)
                else:
                    staging_raw = get_gemini_response(staging_prompt, "gemini-3-flash-preview")
                
                json_str = staging_raw
                if "```json" in json_str: json_str = json_str.split("```json")[1].split("```")[0]
                staging_json = json.loads(json_str.strip())
                new_content = staging_json.get("new_content")
                summary = staging_json.get("summary", "Update staged.")
                
                if new_content:
                    # Base64 encode content to prevent HTML/Quote mangling in the form
                    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
                    
                    confirm_html = f"""
                    <div class="message-agent p-4 glass border-l-4 border-teal rounded-xl max-w-[85%]">
                        <div class="flex justify-between items-start mb-4">
                            <div>
                                <h3 class="font-bold text-navy">Staging Plan Ready</h3>
                                <p class="text-xs text-navy/70 italic">File: <code>{file_to_change}</code></p>
                            </div>
                            <span class="px-2 py-1 bg-teal/10 text-teal text-[10px] font-bold rounded uppercase tracking-wider">Step 1: Review</span>
                        </div>
                        
                        <div class="markdown-content text-sm mb-6">
                            {summary}
                        </div>

                        <div class="flex space-x-3">
                            <form hx-post="/agent/confirm-stage" hx-target="closest .message-agent" hx-swap="outerHTML" class="flex-1">
                                <input type="hidden" name="message_id" value="{message_id}">
                                <input type="hidden" name="file" value="{file_to_change}">
                                <input type="hidden" name="summary" value="{summary}">
                                <input type="hidden" name="content_b64" value="{encoded_content}">
                                <button type="submit" class="w-full bg-teal hover:bg-teal-700 text-white font-bold py-2 px-4 rounded-lg transition-colors shadow-md">Confirm & Stage</button>
                            </form>
                            <button hx-post="/agent/cancel-plan" hx-target="closest .message-agent" hx-swap="outerHTML" class="px-4 py-2 border border-navy/20 text-navy/60 hover:bg-navy/5 rounded-lg transition-colors font-medium text-sm">Discard</button>
                        </div>
                    </div>
                    """
                    await chat_manager.update_message(message_id, confirm_html)
                    # LOCK PERSISTS until user confirms/discards
                else:
                    await chat_manager.update_message(message_id, "Error: Failed to generate content.")
                    await chat_manager.release_lock(user)
            except Exception as e:
                print(f"Write Error: {e}")
                await chat_manager.update_message(message_id, f"Error during staging: {e}")
                await chat_manager.release_lock(user)
        else:
            # READ Logic
            query_prompt = f"""
            You are the CLIA Content Steward. 
            Answer the user's question based on the site context.
            
            IMPORTANT: You are in READ-ONLY mode. You cannot modify files. 
            If the user is asking for a change or update, you must inform them that you misclassified their intent 
            and ask them to rephrase their request more clearly as a modification.
            
            Question: {message}
            Git Context: {git_context}
            Context: {history_context}
            """
            answer = get_gemini_response(query_prompt, model_to_use)
            final_html = f"""
            <div class="message-agent p-4 glass rounded-2xl rounded-tl-none max-w-[85%]">
                <div class="text-[8px] text-navy/30 mb-1 uppercase font-bold tracking-widest">{model_to_use}</div>
                <div class="markdown-content text-navy">
                    {answer}
                </div>
                <span class="text-[10px] text-navy/50 mt-2 block">Agent • Just now</span>
            </div>
            """
            await chat_manager.update_message(message_id, final_html)
            await chat_manager.release_lock(user)

    except Exception as e:
        print(f"Worker Error: {e}")
        await chat_manager.update_message(message_id, f"System Error: {e}")
        await chat_manager.release_lock(user)

@app.post("/agent/cancel-plan")
async def cancel_plan(user: str = Depends(get_iap_user)):
    try:
        return HTMLResponse(content="""
        <div class='message-agent p-4 glass rounded-2xl rounded-tl-none max-w-[85%]'>
            <p class='text-xs italic text-navy/60'>Update plan discarded. No changes were made to the site. Is there anything else I can help you with?</p>
            <span class="text-[10px] text-navy/50 mt-2 block">System • Just now</span>
        </div>
        """)
    finally:
        await chat_manager.release_lock(user)

@app.post("/agent/chat")
async def chat_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    message: str = Form(...),
    file: Optional[UploadFile] = File(None),
    user: str = Depends(get_iap_user)
):
    # Check persistent lock
    lock_status = await chat_manager.is_locked(user)
    if lock_status["locked"]:
        return HTMLResponse(content=f"""
        <div class="message-agent p-4 glass border-l-4 border-sunset rounded-xl max-w-[85%]">
            <div class="flex items-center space-x-2 mb-2 text-sunset">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"></path></svg>
                <p class="text-sm font-bold uppercase tracking-wider">Agent Busy</p>
            </div>
            <p class="text-sm text-navy/70 mb-4"><b>{lock_status['user']}</b> is currently staging an update. Please wait or force an unlock if this is an error.</p>
            <button hx-post="/agent/unlock" 
                    hx-target="closest .message-agent"
                    hx-swap="outerHTML"
                    class="w-full bg-slate hover:bg-slate-200 text-navy/50 text-[10px] font-bold py-2 rounded-lg border border-navy/10 uppercase tracking-widest transition-colors">
                Force Unlock
            </button>
        </div>
        """)

    # Retrieve persistent chat history
    history = await chat_manager.get_recent_messages(user, limit=10)
    history_context = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in history])

    doc_bytes = parse_document(file) if file and file.filename else None
    await chat_manager.save_message(user, "user", message)
    message_id = await chat_manager.save_message(user, "assistant", "Processing...")
    background_tasks.add_task(chat_worker, user, message, doc_bytes, message_id, history_context)
    
    return HTMLResponse(content=f"""
    <div class="flex justify-end mb-6">
        <div class="max-w-[85%] bg-navy text-white p-4 rounded-2xl rounded-tr-none shadow-lg">
            <p class="text-sm leading-relaxed">{message}</p>
            <span class="text-[10px] text-white/50 mt-2 block text-right">You • Just now</span>
        </div>
    </div>
    <div class="flex justify-start" id="status-container-{message_id}" hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <div class="max-w-[85%] glass p-4 rounded-2xl rounded-tl-none flex items-center space-x-3">
            <div class="animate-spin h-4 w-4 border-2 border-teal border-t-transparent rounded-full"></div>
            <p class="text-xs font-bold text-navy/50 uppercase tracking-widest">Agent is thinking...</p>
        </div>
    </div>
    """)

@app.get("/agent/deploy-status")
async def get_deploy_status(branch: str, version: Optional[str] = None, user: str = Depends(get_iap_user)):
    try:
        service_name = "clia-dev" if branch == "dev" else "clia-website"
        project_id = "clia-web-prod"
        region = "us-east1"
        
        # 1. Fetch Cloud Run Service Status
        credentials, _ = google.auth.default()
        auth_request = google.auth.transport.requests.Request()
        credentials.refresh(auth_request)
        url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_id}/services/{service_name}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {credentials.token}"})
        
        if resp.status_code != 200: 
            return HTMLResponse(content="<span class='status-pill bg-gray-100 text-gray-400'>Error</span>")
        
        data = resp.json()
        status_data = data.get("status", {})
        metadata = data.get("metadata", {})
        
        # 2. Spec Sync Check
        obs_gen = status_data.get("observedGeneration", 0)
        meta_gen = metadata.get("generation", 0)
        if obs_gen < meta_gen:
            return HTMLResponse(content="<span class='status-pill status-pulse-blue'>● Triggered</span>")
            
        # 3. Revision Health Check
        latest_created = status_data.get("latestCreatedRevisionName")
        latest_ready = status_data.get("latestReadyRevisionName")
        if latest_created != latest_ready:
            return HTMLResponse(content="<span class='status-pill status-pulse-yellow'>● Deploying</span>")
            
        # 4. Traffic Split Check
        traffic = status_data.get("traffic", [])
        latest_traffic = next((t for t in traffic if t.get("revisionName") == latest_ready), None)
        if not latest_traffic or latest_traffic.get("percent", 0) < 100:
            return HTMLResponse(content="<span class='status-pill status-pulse-orange'>● Activating</span>")
            
        # 5. Version-Aware External Check
        if version:
            try:
                site_url = git_ops.get_dev_url() if branch == "dev" else git_ops.get_prod_url()
                v_resp = requests.get(f"{site_url}/version.json", timeout=3)
                if v_resp.status_code == 200:
                    live_version = str(v_resp.json().get("version"))
                    if live_version != str(version):
                        return HTMLResponse(content="<span class='status-pill status-pulse-orange'>● Propagating</span>")
                else:
                    # If version.json is missing but we expect it, we are still propagating
                    return HTMLResponse(content="<span class='status-pill status-pulse-orange'>● Initializing</span>")
            except Exception:
                # If site is down, we are definitely not live
                return HTMLResponse(content="<span class='status-pill status-pulse-blue'>● Connecting</span>")

        # 6. Final Ready Check
        ready_cond = next((c for c in status_data.get("conditions", []) if c["type"] == "Ready"), None)
        if ready_cond and ready_cond.get("status") == "True":
            return HTMLResponse(content="<span class='status-pill bg-green-100 text-green-700'>● Live</span>")
            
        return HTMLResponse(content="<span class='status-pill bg-red-100 text-red-700'>● Failed</span>")
    except Exception as e:
        print(f"Status Error: {e}")
        return HTMLResponse(content="<span class='status-pill bg-gray-100 text-gray-400'>Error</span>")

@app.post("/agent/confirm-stage")
async def confirm_stage(
    message_id: str = Form(...), 
    file: str = Form(...), 
    summary: str = Form(...), 
    content_b64: str = Form(...), 
    user: str = Depends(get_iap_user)
):
    try:
        # Decode the content
        content = base64.b64decode(content_b64).decode('utf-8')
        print(f"DEBUG: confirm_stage - User: {user}, File: {file}, Content Length: {len(content)}")
        
        git_ops.sync_main()
        branch_name = git_ops.create_content_branch(f"update-{int(time.time())}")
        
        full_path = get_safe_path(REPO_PATH, file)
            
        print(f"DEBUG: confirm_stage - Final Full Path: {full_path}")
        
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding='utf-8')
        
        # Generate version manifest for deployment tracking
        version_path = get_safe_path(REPO_PATH, "version.json")
        commit_ts = int(time.time())
        version_path.write_text(json.dumps({"version": commit_ts, "user": user}), encoding='utf-8')
        
        pushed = git_ops.commit_and_push(f"Agent Update: {summary} (Requested by {user})")
        
        if not pushed:
            error_html = f"""
            <div class="message-agent p-3 rounded-lg max-w-[80%] bg-red-50 border border-red-200">
                <h3 class="font-bold text-red-800">Staging Failed</h3>
                <p class="text-sm">No changes were detected between your request and the current site content. This usually means the update has already been applied or the generated content was identical to the original.</p>
                <button hx-post="/agent/unlock" hx-target="closest .message-agent" hx-swap="outerHTML" class="mt-2 bg-gray-400 hover:bg-gray-500 text-white text-xs font-bold py-1 px-3 rounded">Reset Agent</button>
            </div>
            """
            await chat_manager.update_message(message_id, error_html)
            return HTMLResponse(content=error_html)

        final_html = f"""
        <div class="message-agent p-4 glass border-l-4 border-teal rounded-xl max-w-[85%]">
            <div class="flex justify-between items-start mb-4">
                <div>
                    <h3 class="font-bold text-navy">Staged on Dev Site</h3>
                    <p class="text-xs text-navy/70 italic">{summary}</p>
                </div>
                <span class="px-2 py-1 bg-teal/10 text-teal text-[10px] font-bold rounded uppercase tracking-wider">Step 2: Verify</span>
            </div>

            <div class="flex flex-col space-y-4">
                <div class="flex items-center space-x-3">
                    <a href="{git_ops.get_dev_url()}" target="_blank" class="flex-1 bg-teal hover:bg-teal-700 text-white text-center text-xs font-bold py-2 px-4 rounded-lg transition-colors shadow-md no-underline">Verify on Dev Site</a>
                    <div id="dev-deploy-status" hx-get="/agent/deploy-status?branch=dev&version={commit_ts}" hx-trigger="load, every 10s" class="status-pill bg-slate text-navy/50 border border-navy/10">Checking...</div>
                </div>
                
                <form hx-post="/agent/rollback-dev" hx-target="closest .message-agent" hx-swap="outerHTML">
                    <input type="hidden" name="message_id" value="{message_id}">
                    <button type="submit" class="w-full bg-sunset hover:bg-orange-600 text-white text-[10px] font-bold py-1.5 px-3 rounded-lg uppercase tracking-wider shadow-sm transition-colors">Undo Last (Restore Dev)</button>
                </form>

                <div class="border-t border-navy/10 pt-4">
                    <p class="text-[10px] text-navy/40 mb-2 font-bold uppercase tracking-widest">Final Action</p>
                    <button hx-post="/agent/approve?branch={branch_name}&message_id={message_id}&version={commit_ts}" 
                            hx-target="closest .message-agent" 
                            hx-swap="outerHTML" 
                            class="w-full bg-navy hover:bg-blue-900 text-white font-bold py-2.5 rounded-lg transition-all shadow-lg">
                        Approve & Push to Production
                    </button>
                </div>
            </div>
        </div>
        """

        await chat_manager.update_message(message_id, final_html)
        return HTMLResponse(content=final_html)
    except Exception as e: return HTMLResponse(content=f"<div class='text-red-600'>Error: {e}</div>")

@app.post("/agent/rollback-dev")
async def rollback_dev_endpoint(
    background_tasks: BackgroundTasks,
    message_id: str = Form(...),
    user: str = Depends(get_iap_user)
):
    background_tasks.add_task(rollback_dev_worker, user, message_id)
    return HTMLResponse(content=f"""
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-orange-50 border border-orange-200"
         id="status-container-{message_id}"
         hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <p class="text-sm text-orange-800 animate-pulse">● Rolling back dev site...</p>
    </div>
    """)

@app.post("/agent/approve")
async def approve_update(branch: str, message_id: str, version: Optional[str] = None, user: str = Depends(get_iap_user)):
    try:
        git_ops.merge_to_main(branch)
        version_param = f"&version={version}" if version else ""
        return HTMLResponse(content=f"""
        <div class="message-agent p-4 glass border-l-4 border-navy rounded-xl max-w-[85%]">
            <div class="flex justify-between items-start mb-4">
                <div>
                    <h3 class="font-bold text-navy">Pushing to Production...</h3>
                    <p class="text-xs text-navy/70 italic">Changes merged to <b>main</b></p>
                </div>
                <span class="px-2 py-1 bg-navy/10 text-navy text-[10px] font-bold rounded uppercase tracking-wider">Live Update</span>
            </div>

            <div class="flex flex-col space-y-4">
                <div class="flex items-center space-x-3">
                    <div id="prod-deploy-status" hx-get="/agent/deploy-status?branch=main{version_param}" hx-trigger="load, every 10s" class="status-pill bg-slate text-navy/50 border border-navy/10">Checking Prod...</div>
                    <form hx-post="/agent/revert" hx-target="closest .message-agent" hx-swap="outerHTML" class="flex-1">
                        <input type="hidden" name="message_id" value="{message_id}">
                        <button type="submit" class="w-full text-[10px] text-sunset hover:text-orange-700 underline font-bold uppercase tracking-widest transition-colors">Undo Last (Emergency Revert)</button>
                    </form>
                </div>
            </div>
        </div>
        """)
    except Exception as e: 
        return HTMLResponse(content=f"<div class='text-red-600'>Error: {e}</div>")
    finally:
        await chat_manager.release_lock(user)

@app.post("/agent/revert")
async def revert_update(
    background_tasks: BackgroundTasks,
    message_id: str = Form(...),
    user: str = Depends(get_iap_user)
):
    background_tasks.add_task(rollback_prod_worker, user, message_id)
    return HTMLResponse(content=f"""
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-red-50 border border-red-200"
         id="status-container-{message_id}"
         hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <p class="text-sm text-red-800 animate-pulse">● Rolling back production site...</p>
    </div>
    """)


@app.post("/agent/unlock")
async def force_unlock(user: str = Depends(get_iap_user)):
    await chat_manager.release_lock(user)
    # Return HTML if requested by HTMX, otherwise JSON
    return HTMLResponse(content="""
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-green-50 border border-green-200">
        <p class="text-xs text-green-700 italic">Agent manually unlocked. You can now send a new request.</p>
    </div>
    """)

@app.get("/agent/reset")
async def reset_user_session(user: str = Depends(get_iap_user)):
    """
    Emergency reset for a user's session.
    Clears history and releases the lock.
    """
    await chat_manager.clear_history(user)
    await chat_manager.release_lock(user)
    return HTMLResponse(content="""
    <div style="font-family: sans-serif; padding: 2rem; text-align: center;">
        <h1 style="color: #1e3a8a;">Session Reset Successful</h1>
        <p>Your chat history has been cleared and the agent lock has been released.</p>
        <a href="/agent" style="display: inline-block; background: #1e3a8a; color: white; padding: 0.5rem 1rem; border-radius: 5px; text-decoration: none; margin-top: 1rem;">Return to Switchboard</a>
    </div>
    """)

@app.post("/agent/clear")
async def clear_chat_history(user: str = Depends(get_iap_user)):
    await chat_manager.clear_history(user)
    return {"status": "success", "message": "Chat history cleared."}
