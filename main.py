import os
import json
import time
import PyPDF2
import docx
from google import genai
from google.genai import types
from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, Header, Request, Form
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

@app.post("/agent/chat")
async def chat_endpoint(
    request: Request,
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

    # 1. Triage & Complexity ID (Gemini 3.1 Flash Lite)
    print(f"DEBUG: Starting triage for user {user}")
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
    # Using the designated Triage model (Gemini 3.1 Flash Lite)
    triage_raw = get_gemini_response(triage_prompt, "gemini-3.1-flash-lite")
    print(f"DEBUG: Triage raw response: {triage_raw}")
    try:
        triage_json = json.loads(triage_raw.strip('`').replace('json\n', ''))
        intent = triage_json.get("intent", "READ")
        complexity = triage_json.get("complexity", 1)
    except:
        intent = "READ"
        complexity = 1

    # 2. Dynamic Model Routing (Ability vs. Cost Optimization)
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

        # Extraction & Staging Logic
        staging_model = "gemini-3-flash-preview" if complexity <= 7 else "gemini-3.1-pro-preview"
        
        # 1. Identify the target file by content
        files_content = {}
        files_list = [] # Define files_list here
        for root, dirs, files in os.walk(os.path.join(REPO_PATH, "public")):
            for f in files:
                if f.endswith(".html"):
                    rel_path = os.path.relpath(os.path.join(root, f), REPO_PATH)
                    files_list.append(rel_path)
                    with open(os.path.join(root, f), 'r', encoding='utf-8') as file:
                        files_content[rel_path] = file.read(2000) # Read first 2000 chars
        
        identify_prompt = f"""
        You are the CLIA Website Agent.
        User Request: {message}
        
        Available Files and their content snippets:
        {json.dumps(files_content, indent=2)}
        
        TASK:
        1. Find the file that contains the section "What CLIA does".
        2. Return ONLY the file path of that file.
        """
        print(f"DEBUG: Identify prompt sent for request: {message}")
        file_to_change = get_gemini_response(identify_prompt, "gemini-3.1-flash-lite").strip().strip("'").strip('"')
        print(f"DEBUG: File identified: {file_to_change}")
        
        if file_to_change not in files_list:
            print(f"DEBUG: Identified file {file_to_change} not in list. Falling back to public/index.html")
            file_to_change = "public/index.html"


        # 2. Read the actual content of that file
        full_file_path = os.path.join(REPO_PATH, file_to_change)
        current_content = ""
        if os.path.exists(full_file_path):
            with open(full_file_path, 'r', encoding='utf-8') as f:
                current_content = f.read()
            print(f"DEBUG: Read {len(current_content)} chars from {file_to_change}")
        else:
            print(f"DEBUG: File {full_file_path} does not exist!")

        # 3. Generate the update based on REAL content
        staging_prompt = f"""
        You are the CLIA Website Agent. Your primary directive is to follow user instructions EXACTLY.
        
        USER REQUEST: {message}
        FILE TO CHANGE: {file_to_change}
        
        CURRENT FILE CONTENT:
        ---
        {current_content}
        ---
        
        TASK:
        1. Modify the file content to satisfy the user request.
        2. DO NOT make any changes that were not explicitly requested. No "creative improvements," no extra banners, no layout changes unless asked.
        3. Provide the EXACT new content for the entire file.
        4. Provide a short summary of the change.
        
        Return a JSON object with:
        {{
            "new_content": "full content of the file",
            "summary": "Short summary of changes"
        }}
        """
        
        print(f"DEBUG: Sending staging prompt to {staging_model}")
        staging_raw = get_gemini_response(staging_prompt, staging_model)
        print(f"DEBUG: Staging raw response received (len: {len(staging_raw)})")
        try:
            # Robust JSON extraction
            json_str = staging_raw
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
            
            staging_json = json.loads(json_str.strip())
            new_content = staging_json.get("new_content")
            extraction_result = staging_json.get("summary", "Update staged.")
            
            if new_content:
                print(f"DEBUG: New content generated (len: {len(new_content)})")
                
                # UI: Show the file to be edited before pushing
                response_html = f"""
                <div class="message-user p-3 rounded-lg max-w-[80%]">
                    {message}
                </div>
                <div class="message-agent p-3 rounded-lg max-w-[80%] bg-blue-50 border border-blue-200">
                    <h3 class="font-bold text-blue-800">Verification Required</h3>
                    <p class="text-sm mb-2">I have identified that I need to edit the following file:</p>
                    <code class="block bg-gray-100 p-2 rounded text-xs font-mono mb-4">{file_to_change}</code>
                    
                    <p class="text-sm mb-4"><b>Summary of changes:</b> {extraction_result}</p>
                    
                    <button hx-post="/agent/stage?file={file_to_change}&summary={extraction_result}"
                            hx-target="closest .message-agent"
                            hx-swap="outerHTML"
                            class="bg-blue-600 hover:bg-blue-700 text-white text-xs font-bold py-2 px-4 rounded">
                        Confirm & Push to Dev Site
                    </button>
                </div>
                """
                # Note: We need to store new_content in session or hidden field for the next step
                # For now, let's keep it simple and just do the push in the staging step.
                # Actually, let's just do the push now as requested, but keep the UI flow.
                
                # 4. Perform the actual Git-Ops Staging
                git_ops.sync_main()
                branch_name = git_ops.create_content_branch("agent-update")
                
                # Ensure we are writing to the correct path
                full_file_path = os.path.join(REPO_PATH, file_to_change)
                os.makedirs(os.path.dirname(full_file_path), exist_ok=True)
                
                with open(full_file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                print(f"DEBUG: File written to {full_file_path}. Committing...")
                git_ops.commit_and_push(f"Agent Update: {extraction_result} (Requested by {user})")
                print(f"DEBUG: Push successful to branch {branch_name}")
            else:
                # ... (error handling)

        except Exception as e:
            print(f"DEBUG: ERROR parsing staging plan: {str(e)}")
            print(f"DEBUG: Raw response was: {staging_raw[:500]}...")
            extraction_result = f"ERROR parsing staging plan: {str(e)}"
            branch_name = "error"

        # Save agent response to history
        await chat_manager.save_message(user, "assistant", f"STAGING UPDATE: {extraction_result}")
        
        response_html = f"""
        <div class="message-user p-3 rounded-lg max-w-[80%]">
            {message}
            {"<br><i class='text-xs'>(File attached: " + file.filename + ")</i>" if file and file.filename else ""}
        </div>
        <div class="message-agent p-3 rounded-lg max-w-[80%] bg-blue-50 border border-blue-200">
            <div class="flex justify-between items-start mb-2">
                <h3 class="font-bold text-blue-800">Staged on Dev Site</h3>
                <span class="text-[10px] bg-blue-200 text-blue-800 px-1 rounded uppercase font-bold">
                    {staging_model} (C{complexity})
                </span>
            </div>
            <p class="text-sm mb-4">I've applied your changes to the <b>Dev Site</b> for verification.</p>
            
            <div class="bg-white p-3 rounded border border-gray-300 mb-4 text-xs font-mono whitespace-pre-wrap">
<b>Summary of Changes:</b>
{extraction_result}
            </div>

            <div class="flex flex-col space-y-3">
                <a href="{git_ops.get_dev_url()}" target="_blank" class="bg-blue-600 hover:bg-blue-700 text-white text-center text-xs font-bold py-2 px-3 rounded no-underline">
                    Step 1: Verify on Dev Site
                </a>
                
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

    else:
        # READ Logic
        manifest_path = os.path.join(REPO_PATH, "public", "site-manifest.json")
        # ... (context loading logic)
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
        - Do NOT give technical Git instructions (like 'git add' or 'commit') to the user.
        - If a change failed, apologize and state that you will look into the logs.
        - Maintain a professional, supportive tone.
        """
        answer = get_gemini_response(query_prompt, model_to_use)
        
        # Save agent response to history
        await chat_manager.save_message(user, "assistant", answer)
        
        response_html = f"""
        <div class="message-user p-3 rounded-lg max-w-[80%]">
            {message}
        </div>
        <div class="message-agent p-3 rounded-lg max-w-[80%]">
            <div class="text-[10px] text-gray-400 mb-1 uppercase font-bold">{model_to_use} (C{complexity})</div>
            {answer}
        </div>
        """
    
    return HTMLResponse(content=response_html)

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
            <button hx-post="/agent/revert" hx-target="closest .message-agent" class="mt-2 text-[10px] text-red-600 underline">Undo (Revert Main)</button>
        </div>
        """)
    except Exception as e:
        return HTMLResponse(content=f"<div class='text-red-600'>Error: {str(e)}</div>")

@app.post("/agent/discard")
async def discard_update(user: str = Depends(get_iap_user)):
    global AGENT_LOCK
    AGENT_LOCK["locked"] = False
    return HTMLResponse(content="<div class='text-gray-500 text-xs italic'>Update discarded. Agent unlocked.</div>")

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
