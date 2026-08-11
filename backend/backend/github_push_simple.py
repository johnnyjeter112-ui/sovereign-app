"""
Simple FastAPI server to receive tasks (JSON) and push a timestamped snapshot
to a GitHub repo as a new branch + open a Pull Request.

Quick: set env vars GITHUB_TOKEN and PUSH_API_KEY and run:
uvicorn backend.github_push_simple:app --host 0.0.0.0 --port 8000
"""
import os, time, base64, json
import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Any, Dict

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")            # PAT with repo write & pull_request permissions
PUSH_API_KEY = os.getenv("PUSH_API_KEY")            # server-side API key to protect endpoint

if not GITHUB_TOKEN or not PUSH_API_KEY:
    raise RuntimeError("Set GITHUB_TOKEN and PUSH_API_KEY environment variables before running")

app = FastAPI(title="Simple GitHub Push Service")

class PushRequest(BaseModel):
    repo: str             # "owner/repo"
    base_branch: str = "main"
    tasks: Dict[str, Any] # will be saved as JSON

def gh_request(method: str, path: str, **kwargs):
    url = f"{GITHUB_API}{path}"
    headers = kwargs.pop("headers", {})
    headers.update({
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    })
    resp = requests.request(method, url, headers=headers, **kwargs)
    if not resp.ok:
        raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text}")
    return resp.json()

@app.post("/push")
def push(payload: PushRequest, x_api_key: str = Header(None)):
    if x_api_key != PUSH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if "/" not in payload.repo:
        raise HTTPException(status_code=400, detail="repo must be owner/repo")

    owner, repo = payload.repo.split("/", 1)

    # 1) Get base branch SHA
    try:
        ref = gh_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{payload.base_branch}")
        base_sha = ref["object"]["sha"]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Base branch check failed: {e}")

    # 2) Create new branch
    ts = time.strftime("%Y%m%d-%H%M%S")
    new_branch = f"push-tasks-{ts}"
    try:
        gh_request("POST", f"/repos/{owner}/{repo}/git/refs", json={
            "ref": f"refs/heads/{new_branch}",
            "sha": base_sha
        })
    except Exception as e:
        # if branch exists, continue
        if "Reference already exists" not in str(e):
            raise HTTPException(status_code=500, detail=f"Create branch failed: {e}")

    # 3) Create file on branch
    snapshot = {"exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "tasks": payload.tasks}
    content_str = json.dumps(snapshot, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
    file_path = f"tasks/tasks-{ts}.json"
    commit_message = f"Add tasks snapshot {ts}"
    try:
        gh_request("PUT", f"/repos/{owner}/{repo}/contents/{file_path}", json={
            "message": commit_message,
            "content": content_b64,
            "branch": new_branch
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create file failed: {e}")

    # 4) Open PR
    pr_title = f"Add tasks snapshot ({ts})"
    pr_body = "Snapshots created by the To-Do app via server push."
    try:
        pr = gh_request("POST", f"/repos/{owner}/{repo}/pulls", json={
            "title": pr_title,
            "head": new_branch,
            "base": payload.base_branch,
            "body": pr_body
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Open PR failed: {e}")

    return {"pr_url": pr.get("html_url"), "branch": new_branch, "file": file_path}
