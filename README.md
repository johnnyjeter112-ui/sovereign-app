# Professional PWA To‑Do + Server Push (snapshot → GitHub PR)

This repo contains a mobile-first progressive web app (PWA) To‑Do application and a small FastAPI server that can push a timestamped tasks snapshot to a GitHub repository as a new branch and open a Pull Request.

Quick overview
- Frontend: frontend/index.html — mobile-first PWA optimized for iPhone 17 Pro (touch-friendly, Add to Home Screen).
- Backend: backend/github_push_simple.py — FastAPI endpoint POST /push which uses a server-side GitHub token to create branch, commit tasks JSON, and open PR.
- CI: .github/workflows/ci.yml — simple sanity checks.
- Local dev: docker-compose.yaml for a quick containerized run.

Files to create (branch: feature/professional-mas)
- README.md (this file)
- .gitignore
- docker-compose.yaml
- backend/requirements.txt
- backend/github_push_simple.py
- .github/workflows/ci.yml
- frontend/index.html
- frontend/manifest.json
- frontend/sw.js

Environment variables (server)
- GITHUB_TOKEN — GitHub Personal Access Token (classic or fine-grained) with:
  - repo:contents write (or Contents: Read & write) and Pull requests: Read & write
- PUSH_API_KEY — a strong secret used by the frontend when calling the /push endpoint

Local run (simple)
1. Install Python deps:
   pip install -r backend/requirements.txt

2. Run the server:
   export GITHUB_TOKEN="ghp_..."
   export PUSH_API_KEY="pick-a-strong-secret"
   uvicorn backend.github_push_simple:app --host 0.0.0.0 --port 8000

3. Open frontend/index.html in a static host or open it locally (file:// or via a static server). In production, serve it from Vercel/Netlify.

Quick test (curl)
curl -X POST "http://localhost:8000/push" \
  -H "Content-Type: application/json" \
  -H "x-api-key: pick-a-strong-secret" \
  -d '{"repo":"<owner>/<repo>","base_branch":"main","tasks":[{"id":"1","title":"Test","done":false}]}'

Result: {"pr_url":"https://github.com/...","branch":"push-tasks-...","file":"tasks/tasks-....json"}

Want me to deploy this for you? Tell me:
- I will prepare a Render/Vercel one-click guide and provide the deploy steps you can complete from your iPhone in minutes.
