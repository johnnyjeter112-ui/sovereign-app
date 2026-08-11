"""
Enhanced FastAPI server with production hardening:
- CORS origins read from ALLOWED_ORIGINS env var (comma separated). Require explicit setting in production.
- Optional wildcard allowed when ALLOW_ORIGIN_WILDCARD=1 (explicit opt-in).
- Rotating file logging with simple JSON formatter
- Request ID middleware (X-Request-ID) and per-request logging
- Body size limit middleware (MAX_REQUEST_BODY_BYTES env, default 200_000)
- Simple in-memory rate limiting per client IP (RATE_LIMIT_PER_MIN)
- /health and /metrics endpoints (basic)
- Incremental counters for pushes and failures
- Improved structured logging for GitHub API failures
"""
import os
import time
import base64
import json
import logging
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Config from environment
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")            # PAT with repo write & pull_request permissions
PUSH_API_KEY = os.getenv("PUSH_API_KEY")            # server-side API key to protect endpoint
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS")
ALLOW_ORIGIN_WILDCARD = os.getenv("ALLOW_ORIGIN_WILDCARD", "0") == "1"
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", "200000"))
LOG_FILE = os.getenv("LOG_FILE", "logs/github_push_service.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "1048576"))  # 1MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

if not GITHUB_TOKEN or not PUSH_API_KEY:
    raise RuntimeError("Set GITHUB_TOKEN and PUSH_API_KEY environment variables before running")

# ALLOWED_ORIGINS must be explicitly set in production
if not ALLOWED_ORIGINS and not ALLOW_ORIGIN_WILDCARD:
    raise RuntimeError("Set ALLOWED_ORIGINS (comma-separated) or set ALLOW_ORIGIN_WILDCARD=1 for wildcard (not recommended)")

# Ensure log directory exists
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Basic structured JSON logger (simple, no external deps)
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # include extra attributes if present
        for k in ("request_id", "client_ip", "repo", "branch"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)

logger = logging.getLogger("github_push_service")
logger.setLevel(logging.INFO)

# Console handler (human-friendly)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logger.addHandler(ch)

# Rotating file handler with JSON formatter
fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
fh.setLevel(logging.INFO)
fh.setFormatter(JSONFormatter())
logger.addHandler(fh)

app = FastAPI(title="GitHub Push Service (production-hardened)")

# CORS middleware - read allowed origins from env (comma-separated)
if ALLOW_ORIGIN_WILDCARD:
    origins = ["*"]
else:
    origins = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple runtime counters
_start_time = time.time()
_total_pushes = 0
_total_errors = 0

# Simple in-memory rate limiter (per-client IP): sliding window timestamps
_rate_limits: Dict[str, list] = {}

class PushRequest(BaseModel):
    repo: str             # "owner/repo"
    base_branch: str = "main"
    tasks: Any            # will be saved as JSON


# Middleware: request id + body size limit + attach client ip + rate limiting
@app.middleware("http")
async def request_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    client_ip = request.client.host if request.client else "unknown"

    # read body bytes to enforce limit
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY_BYTES:
        logger.warning("Request body too large", extra={"request_id": request_id, "client_ip": client_ip})
        return JSONResponse({"detail": "Request body too large"}, status_code=413)

    # Rate limit: allow RATE_LIMIT_PER_MIN requests per minute per client
    now = time.time()
    window_start = now - 60
    timestamps = _rate_limits.get(client_ip, [])
    # remove old timestamps
    timestamps = [t for t in timestamps if t >= window_start]
    if len(timestamps) >= RATE_LIMIT_PER_MIN:
        logger.warning("Rate limit exceeded", extra={"request_id": request_id, "client_ip": client_ip})
        return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    timestamps.append(now)
    _rate_limits[client_ip] = timestamps

    # attach request_id to state so handlers can use it
    request.state.request_id = request_id
    request.state.client_ip = client_ip

    try:
        response: Response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error during request", extra={"request_id": request_id, "client_ip": client_ip})
        raise
    response.headers["X-Request-ID"] = request_id
    return response


def gh_request(method: str, path: str, **kwargs):
    url = f"{GITHUB_API}{path}"
    headers = kwargs.pop("headers", {})
    headers.update({
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    })
    resp = requests.request(method, url, headers=headers, **kwargs)
    if not resp.ok:
        # log details for debugging
        logger.error("GitHub API error %s %s -> %s: %s", method, url, resp.status_code, resp.text)
        raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text}")
    logger.info("GitHub API %s %s -> %s", method, url, resp.status_code)
    return resp.json()


@app.post("/push")
async def push(request: Request, payload: PushRequest, x_api_key: str = Header(None)):
    global _total_pushes, _total_errors
    request_id = getattr(request.state, "request_id", None)
    client_ip = getattr(request.state, "client_ip", None)

    extra = {"request_id": request_id, "client_ip": client_ip, "repo": getattr(payload, "repo", None)}

    # Basic auth by API key
    if x_api_key != PUSH_API_KEY:
        logger.warning("Invalid API key attempt", extra=extra)
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not payload.repo or "/" not in payload.repo:
        raise HTTPException(status_code=400, detail="repo must be owner/repo")

    owner, repo = payload.repo.split("/", 1)

    # Log incoming push (do not log full task contents in production)
    try:
        task_count = len(payload.tasks) if isinstance(payload.tasks, (list, dict)) else 1
    except Exception:
        task_count = -1
    logger.info("Received push request", extra={**extra, "tasks": task_count, "branch": payload.base_branch})

    # 1) Get base branch SHA
    try:
        ref = gh_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{payload.base_branch}")
        base_sha = ref["object"]["sha"]
    except Exception as e:
        logger.exception("Base branch lookup failed", extra=extra)
        _total_errors += 1
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
            logger.exception("Create branch failed", extra=extra)
            _total_errors += 1
            raise HTTPException(status_code=500, detail=f"Create branch failed: {e}")
        else:
            logger.info("Branch %s already exists, continuing", new_branch, extra=extra)

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
        logger.exception("Create file failed", extra=extra)
        _total_errors += 1
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
        logger.exception("Open PR failed", extra=extra)
        _total_errors += 1
        raise HTTPException(status_code=500, detail=f"Open PR failed: {e}")

    _total_pushes += 1
    pr_url = pr.get("html_url")
    logger.info("Created PR", extra={**extra, "pr_url": pr_url, "branch": new_branch})
    return {"pr_url": pr_url, "branch": new_branch, "file": file_path}


@app.get("/health")
async def health():
    uptime = int(time.time() - _start_time)
    return {"status": "ok", "uptime_seconds": uptime}


@app.get("/metrics")
async def metrics():
    uptime = int(time.time() - _start_time)
    return {
        "uptime_seconds": uptime,
        "total_pushes": _total_pushes,
        "total_errors": _total_errors,
        "allowed_origins": origins,
        "rate_limit_per_min": RATE_LIMIT_PER_MIN,
    }
