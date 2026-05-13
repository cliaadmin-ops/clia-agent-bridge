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

def get_gemini_response(prompt: str, model_name: str = "gemini-3.1-flash-lite", contents: list = None) -> str:
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
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-yellow-50 border border-yellow-200"
         id="status-container-{message_id}"
         hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <p class="text-sm text-yellow-800" id="status-message">{content}</p>
    </div>
    """)

async def chat_worker(user: str, message: str, doc_bytes: Optional[bytes], message_id: str, history_context: str):
    try:
        # Acquire Persistent Lock
        if not await chat_manager.acquire_lock(user):
            await chat_manager.update_message(message_id, "Agent Busy: Another task is in progress.")
            return

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
        
        Site Manifest: {manifest_content}
        User Message: {message}
        
        Rules:
        1. If the user asks to change, update, increment, fix, or modify ANY content (including version numbers, text, or data), intent MUST be 'WRITE'.
        2. If the user asks a question about the site or board, intent is 'READ'.
        3. Respond ONLY with a JSON object: {{"intent": "READ"|"WRITE", "complexity": 1-10, "target_file": "path/to/file"}}
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

        model_to_use = "gemini-3.1-flash-lite" if complexity <= 3 else "gemini-3-flash-preview" if complexity <= 7 else "gemini-3.1-pro-preview"
        
        if intent == "WRITE" or doc_bytes:
            try:
                # 0. Sync Main and Get Context
                await chat_manager.update_message(message_id, "Syncing repository...")
                git_ops.sync_main()
                
                diff_stat = git_ops.repo.git.diff('main..origin/dev', '--stat')
                git_context = f"Baseline: main. Pending on dev: {diff_stat if diff_stat else 'None'}"

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
                    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-blue-50 border border-blue-200">
                        <h3 class="font-bold text-blue-800">Step 1: Verify Selection</h3>
                        <p class="text-sm mb-2">File: <code>{file_to_change}</code></p>
                        <p class="text-xs italic mb-4">Summary: {summary}</p>
                        <div class="flex space-x-2">
                            <form hx-post="/agent/confirm-stage" hx-target="closest .message-agent" hx-swap="outerHTML" class="flex-1">
                                <input type="hidden" name="message_id" value="{message_id}">
                                <input type="hidden" name="file" value="{file_to_change}">
                                <input type="hidden" name="summary" value="{summary}">
                                <input type="hidden" name="content_b64" value="{encoded_content}">
                                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white text-xs font-bold py-2 px-3 rounded">Confirm & Stage</button>
                            </form>
                            <button hx-post="/agent/discard" hx-target="closest .message-agent" hx-swap="outerHTML" class="bg-gray-400 hover:bg-gray-500 text-white text-xs font-bold py-1 px-3 rounded">Discard</button>
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
            Context: {history_context}
            """
            answer = get_gemini_response(query_prompt, model_to_use)
            final_html = f"<div class='message-agent p-3 rounded-lg max-w-[80%]'><div class='text-[10px] text-gray-400 mb-1 uppercase font-bold'>{model_to_use}</div>{answer}</div>"
            await chat_manager.update_message(message_id, final_html)
            await chat_manager.release_lock(user)

    except Exception as e:
        print(f"Worker Error: {e}")
        await chat_manager.update_message(message_id, f"System Error: {e}")
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
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-yellow-50 border border-yellow-200">
            <p class="text-sm text-yellow-800"><b>Agent Busy:</b> {lock_status['user']} is staging an update.</p>
            <button hx-post="/agent/unlock" 
                    hx-target="closest .message-agent"
                    hx-swap="outerHTML"
                    class="mt-2 text-[10px] bg-yellow-200 hover:bg-yellow-300 text-yellow-800 py-1 px-2 rounded border border-yellow-400">
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
    <div class="message-user p-3 rounded-lg max-w-[80%]">{message}</div>
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-yellow-50 border border-yellow-200" id="status-container-{message_id}" hx-get="/agent/status/{message_id}" hx-trigger="every 2s" hx-swap="outerHTML">
        <p class="text-sm text-yellow-800">Processing...</p>
    </div>
    """)

@app.get("/agent/deploy-status")
async def get_deploy_status(branch: str, user: str = Depends(get_iap_user)):
    try:
        service_name = "clia-dev" if branch == "dev" else "clia-website"
        project_id = "clia-web-prod"
        region = "us-east1"
        credentials, _ = google.auth.default()
        auth_request = google.auth.transport.requests.Request()
        credentials.refresh(auth_request)
        url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_id}/services/{service_name}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {credentials.token}"})
        if resp.status_code != 200: return HTMLResponse(content="<span class='status-pill bg-gray-100 text-gray-400'>Error</span>")
        data = resp.json()
        status_data = data.get("status", {})
        obs_gen = status_data.get("observedGeneration")
        meta_gen = data.get("metadata", {}).get("generation")
        ready_cond = next((c for c in status_data.get("conditions", []) if c["type"] == "Ready"), None)
        if not ready_cond: return HTMLResponse(content="<span class='status-pill bg-yellow-100 text-yellow-700 animate-pulse'>● Initializing</span>")
        status = ready_cond.get("status")
        if (obs_gen is not None and meta_gen is not None and obs_gen < meta_gen) or status == "Unknown":
            return HTMLResponse(content="<span class='status-pill bg-yellow-100 text-yellow-700 animate-pulse'>● Deploying...</span>")
        if status == "True": return HTMLResponse(content="<span class='status-pill bg-green-100 text-green-700'>● Live</span>")
        return HTMLResponse(content="<span class='status-pill bg-red-100 text-red-700'>● Failed</span>")
    except Exception: return HTMLResponse(content="<span class='status-pill bg-gray-100 text-gray-400'>Error</span>")

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
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-blue-50 border border-blue-200">
            <h3 class="font-bold text-blue-800">Staged on Dev Site</h3>
            <p class="text-sm mb-4">Summary: {summary}</p>
            <div class="flex flex-col space-y-3">
                <div class="flex items-center space-x-2">
                    <a href="{git_ops.get_dev_url()}" target="_blank" class="flex-1 bg-blue-600 hover:bg-blue-700 text-white text-center text-xs font-bold py-2 px-3 rounded no-underline">Step 1: Verify on Dev Site</a>
                    <div id="dev-deploy-status" hx-get="/agent/deploy-status?branch=dev" hx-trigger="load, every 10s" class="status-pill bg-gray-100 text-gray-500">Checking...</div>
                </div>
                <div class="border-t border-blue-200 pt-3">
                    <p class="text-[10px] text-blue-600 mb-2 font-bold uppercase">Step 2: Final Action</p>
                    <div class="flex space-x-2">
                        <button hx-post="/agent/approve?branch={branch_name}" hx-target="closest .message-agent" hx-swap="outerHTML" class="flex-1 bg-green-600 hover:bg-green-700 text-white text-xs font-bold py-1 px-3 rounded">Approve & Push</button>
                        <button hx-post="/agent/discard" hx-target="closest .message-agent" hx-swap="outerHTML" class="bg-gray-400 hover:bg-gray-500 text-white text-xs font-bold py-1 px-3 rounded">Discard</button>
                    </div>
                </div>
            </div>
        </div>
        """
        await chat_manager.update_message(message_id, final_html)
        return HTMLResponse(content=final_html)
    except Exception as e: return HTMLResponse(content=f"<div class='text-red-600'>Error: {e}</div>")

@app.post("/agent/approve")
async def approve_update(branch: str, user: str = Depends(get_iap_user)):
    try:
        git_ops.merge_to_main(branch)
        return HTMLResponse(content=f"""
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-green-50 border border-green-200">
            <h3 class="font-bold text-green-800 mb-1">Success!</h3>
            <p class="text-sm text-green-700">Changes merged to <b>main</b>.</p>
            <div class="mt-2 flex items-center space-x-2">
                <div id="prod-deploy-status" hx-get="/agent/deploy-status?branch=main" hx-trigger="load, every 10s" class="status-pill bg-white border border-green-200 text-green-600">Checking Prod...</div>
                <button hx-post="/agent/revert" hx-target="closest .message-agent" class="text-[10px] text-red-600 underline">Undo</button>
            </div>
        </div>
        """)
    except Exception as e: 
        return HTMLResponse(content=f"<div class='text-red-600'>Error: {e}</div>")
    finally:
        await chat_manager.release_lock(user)

@app.post("/agent/discard")
async def discard_update(user: str = Depends(get_iap_user)):
    try:
        success = git_ops.discard_dev_changes()
        if success: 
            return HTMLResponse(content="<div class='message-agent p-3 rounded-lg max-w-[80%] bg-gray-50 border border-gray-200'><p class='text-xs italic text-gray-600'>Update discarded. Dev Site reset.</p></div>")
        return HTMLResponse(content="<div class='message-agent p-3 rounded-lg max-w-[80%] bg-red-50 border border-red-200'><p class='text-xs text-red-600'>Reset failed.</p></div>")
    except Exception as e:
        return HTMLResponse(content=f"<div class='text-red-600 text-xs'>Error: {e}</div>")
    finally:
        await chat_manager.release_lock(user)

@app.post("/agent/revert")
async def revert_update(user: str = Depends(get_iap_user)):
    try:
        new_sha = git_ops.revert_main()
        return HTMLResponse(content=f"<div class='message-agent p-3 rounded-lg max-w-[80%] bg-orange-50 border border-orange-200'><p class='text-sm text-orange-800'><b>Reverted.</b> New HEAD: {new_sha[:7]}</p></div>")
    except Exception as e: return HTMLResponse(content=f"<div class='text-red-600'>Revert failed: {e}</div>")

@app.post("/agent/unlock")
async def force_unlock(user: str = Depends(get_iap_user)):
    await chat_manager.release_lock(user)
    # Return HTML if requested by HTMX, otherwise JSON
    return HTMLResponse(content="""
    <div class="message-agent p-3 rounded-lg max-w-[80%] bg-green-50 border border-green-200">
        <p class="text-xs text-green-700 italic">Agent manually unlocked. You can now send a new request.</p>
    </div>
    """)

@app.post("/agent/clear")
async def clear_chat_history(user: str = Depends(get_iap_user)):
    await chat_manager.clear_history(user)
    return {"status": "success", "message": "Chat history cleared."}
