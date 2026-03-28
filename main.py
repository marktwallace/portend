import os
import subprocess
import time
from pathlib import Path
from typing import Optional
import secrets

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

app = FastAPI()
security = HTTPBasic()

HOME = Path.home()
LOGS_DIR = HOME / "logs"
BASE_URL = os.environ.get("SERVER_BASE_URL_PATH", "").rstrip("/")


# --- Auth ---

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    expected_user = os.environ.get("PORTEND_USER", "admin")
    expected_pass = os.environ.get("PORTEND_PASSWORD", "")
    ok = (
        secrets.compare_digest(credentials.username, expected_user)
        and secrets.compare_digest(credentials.password, expected_pass)
    )
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username


# --- App discovery ---

def is_persistent(app_dir: Path) -> bool:
    return (app_dir / "start.sh").exists()


def discover_apps():
    apps = []
    for d in sorted(HOME.iterdir()):
        if d.is_dir() and (d / "refresh.sh").exists():
            apps.append(d)
    return apps


def read_env(app_dir: Path) -> dict:
    env = {}
    env_file = app_dir / ".env"
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def get_status(app_dir: Path) -> dict:
    name = app_dir.name
    pid_file = HOME / f".{name}.pid"
    pid = None
    running = False
    uptime = None

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # check process exists
            running = True
            # get uptime via ps
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etime="],
                capture_output=True, text=True
            )
            uptime = result.stdout.strip() or None
        except (ValueError, ProcessLookupError, PermissionError):
            running = False

    env = read_env(app_dir)
    port = env.get("PORT")
    base_path = env.get("SERVER_BASE_URL_PATH", "")

    # git info
    branch = last_commit = None
    try:
        branch = subprocess.run(
            ["git", "-C", str(app_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True
        ).stdout.strip()
        last_commit = subprocess.run(
            ["git", "-C", str(app_dir), "log", "-1", "--format=%s"],
            capture_output=True, text=True
        ).stdout.strip()
    except Exception:
        pass

    return {
        "name": name,
        "path": str(app_dir),
        "persistent": is_persistent(app_dir),
        "running": running,
        "pid": pid,
        "uptime": uptime,
        "port": port,
        "base_path": base_path,
        "branch": branch,
        "last_commit": last_commit,
    }


def get_log_lines(app_dir: Path, n: int = 200) -> str:
    log_file = LOGS_DIR / f"{app_dir.name}.log"
    if not log_file.exists():
        return "(no log file found)"
    lines = log_file.read_text().splitlines()
    return "\n".join(lines[-n:])


# --- HTML helpers ---

def render_page(request: Request, apps: list, selected: Optional[dict], log: str) -> str:
    app_list_items = ""
    for a in apps:
        dot = "🟢" if a["running"] else ("⚪" if not a["persistent"] else "🔴")
        active = "font-weight:bold;" if selected and a["name"] == selected["name"] else ""
        app_list_items += f'<li style="{active}"><a href="{BASE_URL}/?app={a["name"]}">{dot} {a["name"]}</a></li>\n'

    if selected:
        s = selected
        if s["persistent"]:
            status_text = f'{"Running" if s["running"] else "Stopped"}'
            if s["uptime"]:
                status_text += f" (up {s['uptime']})"
            if s["pid"]:
                status_text += f" · PID {s['pid']}"
        else:
            status_text = "Cron/batch"

        port_text = f" · :{s['port']}" if s["port"] else ""
        branch_text = f" · {s['branch']}" if s["branch"] else ""
        commit_text = f" · {s['last_commit']}" if s["last_commit"] else ""

        refresh_form = f'''
        <form method="post" action="{BASE_URL}/refresh" style="display:inline;">
            <input type="hidden" name="app" value="{s["name"]}">
            <button type="submit">Pull &amp; Restart</button>
        </form>'''

        header = f"<strong>{s['name']}</strong>{port_text}{branch_text}{commit_text} &nbsp; {status_text} &nbsp; {refresh_form}"
        log_html = f'<pre id="log">{_escape(log)}</pre>'
    else:
        header = "<em>Select an app</em>"
        log_html = ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>portend</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ display: flex; height: 100vh; font-family: monospace; font-size: 13px; }}
  #sidebar {{ width: 200px; min-width: 200px; border-right: 1px solid #ccc; padding: 12px; overflow-y: auto; }}
  #sidebar h2 {{ font-size: 13px; margin-bottom: 10px; color: #666; }}
  #sidebar ul {{ list-style: none; }}
  #sidebar li {{ margin: 4px 0; }}
  #sidebar a {{ text-decoration: none; color: #222; }}
  #sidebar a:hover {{ text-decoration: underline; }}
  #main {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
  #header {{ padding: 10px 14px; border-bottom: 1px solid #ccc; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  #logwrap {{ flex: 1; overflow-y: scroll; padding: 10px 14px; background: #f8f8f8; }}
  pre#log {{ white-space: pre-wrap; word-break: break-all; }}
  button {{ font-family: monospace; font-size: 13px; padding: 2px 10px; cursor: pointer; }}
</style>
</head>
<body>
<div id="sidebar">
  <h2>portend</h2>
  <ul>{app_list_items}</ul>
</div>
<div id="main">
  <div id="header">{header}</div>
  <div id="logwrap">{log_html}</div>
</div>
<script>
  // Auto-scroll log to bottom on load
  const lw = document.getElementById("logwrap");
  if (lw) lw.scrollTop = lw.scrollHeight;
</script>
</body>
</html>"""


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- API routes (AI / machine access) ---

@app.get("/api/apps")
async def api_apps(_: str = Depends(check_auth)):
    apps = [get_status(d) for d in discover_apps()]
    return apps


@app.get("/api/log", response_class=PlainTextResponse)
async def api_log(app: str, n: int = 200, _: str = Depends(check_auth)):
    app_dir = HOME / app
    if not app_dir.exists():
        raise HTTPException(status_code=404)
    return get_log_lines(app_dir, n)


@app.post("/api/refresh")
async def api_refresh(request: Request, _: str = Depends(check_auth)):
    body = await request.json()
    app_name = body.get("app")
    if not app_name:
        raise HTTPException(status_code=400, detail="missing 'app' field")
    app_dir = HOME / app_name
    refresh_script = app_dir / "refresh.sh"
    if not refresh_script.exists():
        raise HTTPException(status_code=404, detail=f"no refresh.sh in {app_dir}")
    subprocess.Popen(
        [str(refresh_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"status": "refresh started", "app": app_name}


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, app: Optional[str] = None, _: str = Depends(check_auth)):
    apps = [get_status(d) for d in discover_apps()]
    selected = next((a for a in apps if a["name"] == app), None)
    if selected is None and apps:
        selected = apps[0]
    log = get_log_lines(Path(selected["path"])) if selected else ""
    return render_page(request, apps, selected, log)


@app.post("/refresh")
async def refresh(request: Request, _: str = Depends(check_auth)):
    form = await request.form()
    app_name = form.get("app")
    if not app_name:
        raise HTTPException(status_code=400)
    app_dir = HOME / app_name
    refresh_script = app_dir / "refresh.sh"
    if not refresh_script.exists():
        raise HTTPException(status_code=404)
    subprocess.Popen(
        [str(refresh_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return RedirectResponse(url=f"{BASE_URL}/?app={app_name}", status_code=303)


@app.get("/log", response_class=PlainTextResponse)
async def log(app: str, _: str = Depends(check_auth)):
    app_dir = HOME / app
    if not app_dir.exists():
        raise HTTPException(status_code=404)
    return get_log_lines(app_dir)
