import os
import shlex
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

LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}

def classify_line(line: str) -> str:
    if " ERROR " in line or "ERROR:" in line:
        return "ERROR"
    if " WARNING " in line or " WARN " in line:
        return "WARN"
    if " DEBUG " in line:
        return "DEBUG"
    return "INFO"


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

def unit_name(app_dir: Path) -> str:
    # Directory convention: Dabble_prod -> dabble-prod
    return app_dir.name.lower().replace("_", "-")


def systemctl_show(unit: str) -> dict:
    props = ["LoadState", "ActiveState", "SubState", "MainPID",
             "ActiveEnterTimestamp", "NRestarts"]
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=" + ",".join(props)],
        capture_output=True, text=True,
    )
    out = {}
    for line in result.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def has_unit(app_dir: Path) -> bool:
    # A slot is "persistent" if a systemd unit is installed for it; otherwise it
    # is a cron/batch dir (e.g. the ETL), shown status-less.
    return systemctl_show(unit_name(app_dir)).get("LoadState") == "loaded"


def discover_apps():
    apps = []
    for d in sorted(HOME.iterdir()):
        if d.is_dir() and (d / ".git").exists():
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
    show = systemctl_show(unit_name(app_dir))
    persistent = show.get("LoadState") == "loaded"

    running = failed = False
    pid = None
    uptime = None
    restarts = None
    if persistent:
        active = show.get("ActiveState")
        running = active == "active"
        failed = active == "failed"
        mainpid = show.get("MainPID")
        if mainpid and mainpid != "0":
            pid = int(mainpid)
        restarts = show.get("NRestarts")
        ts = show.get("ActiveEnterTimestamp")
        if running and ts:
            uptime = ts

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
        "persistent": persistent,
        "running": running,
        "failed": failed,
        "pid": pid,
        "uptime": uptime,
        "restarts": restarts,
        "port": port,
        "base_path": base_path,
        "branch": branch,
        "last_commit": last_commit,
    }


def get_log_lines(app_dir: Path, n: int = 200, min_level: str = "INFO") -> str:
    min_val = LOG_LEVELS.get(min_level.upper(), 1)
    if has_unit(app_dir):
        # Persistent app: read the journal for its unit. Fetch a generous window,
        # then apply the same text-based level filter as the file path below
        # (Dabble carries its level in the line text, not journald priority).
        result = subprocess.run(
            ["journalctl", "-u", unit_name(app_dir), "-n", str(max(n, 1000)),
             "-o", "short-iso", "--no-pager"],
            capture_output=True, text=True,
        )
        lines = result.stdout.splitlines()
    else:
        # Batch/cron dir (e.g. the ETL): still logs to a file.
        log_file = LOGS_DIR / f"{app_dir.name}.log"
        if not log_file.exists():
            return "(no log file found)"
        lines = log_file.read_text().splitlines()
    filtered = [l for l in lines if LOG_LEVELS.get(classify_line(l), 1) >= min_val]
    return "\n".join(filtered[-n:])


def _pull_and_restart(app_dir: Path) -> None:
    # Detached so it survives portend restarting itself; systemd (PID 1) carries
    # out the restart independently of this process. Batch dirs (no unit) pull only.
    cmd = f"git -C {shlex.quote(str(app_dir))} pull"
    if has_unit(app_dir):
        cmd += f" ; sudo systemctl restart {shlex.quote(unit_name(app_dir))}"
    subprocess.Popen(
        ["/bin/bash", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- HTML helpers ---

def render_page(request: Request, apps: list, selected: Optional[dict], log: str, level: str = "INFO") -> str:
    app_list_items = ""
    for a in apps:
        dot = "🟢" if a["running"] else ("⚪" if not a["persistent"] else "🔴")
        active = "font-weight:bold;" if selected and a["name"] == selected["name"] else ""
        app_list_items += f'<li style="{active}"><a href="{BASE_URL}/?app={a["name"]}">{dot} {a["name"]}</a></li>\n'

    if selected:
        s = selected
        if s["persistent"]:
            if s["running"]:
                status_text = "Running"
                if s["uptime"]:
                    status_text += f" since {s['uptime']}"
            elif s.get("failed"):
                status_text = "FAILED"
            else:
                status_text = "Stopped"
            if s.get("restarts") and s["restarts"] not in ("0", None):
                status_text += f" · {s['restarts']} restarts"
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

        level_links = " ".join(
            f'<a href="{BASE_URL}/?app={s["name"]}&level={lv}" '
            f'style="{"font-weight:bold;text-decoration:underline;" if lv == level.upper() else ""}">{lv}</a>'
            for lv in LOG_LEVELS
        )
        refresh_log_link = f'<a href="{BASE_URL}/?app={s["name"]}&level={level.upper()}">Refresh log</a>'
        header = f"<strong>{s['name']}</strong>{port_text}{branch_text}{commit_text} &nbsp; {status_text} &nbsp; {refresh_form} &nbsp; <small>{refresh_log_link} &nbsp; level: {level_links}</small>"
        log_html = f'<pre id="log">{_escape(log)}</pre>'
    else:
        header = "<em>Select an app</em>"
        log_html = ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>App Dashboard</title>
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
  <h2><a href="{BASE_URL}/">Refresh app list</a></h2>
  <ul>{app_list_items}</ul>
  <p style="margin-top:14px;border-top:1px solid #ccc;padding-top:8px;"><a href="{BASE_URL}/help">Help</a></p>
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


def render_help() -> str:
    # Plain string (not an f-string) so the CSS braces need no escaping; only the
    # {BASE_URL} token is substituted. Kept domain-neutral: no app/host names.
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>portend — help</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: monospace; font-size: 13px; line-height: 1.5; padding: 20px 28px; max-width: 820px; }
  h1 { font-size: 16px; margin-bottom: 6px; }
  h2 { font-size: 13px; margin: 18px 0 6px; color: #444; }
  p { margin: 6px 0; }
  ul { margin: 6px 0 6px 20px; }
  code { background: #f0f0f0; padding: 1px 4px; }
  pre { background: #f6f6f6; padding: 8px 10px; margin: 6px 0; overflow-x: auto; }
  a { color: #06c; }
  .dot { font-size: 15px; }
</style>
</head>
<body>
<p><a href="{BASE_URL}/">&larr; back to dashboard</a></p>
<h1>How portend works</h1>
<p>portend lists each app (a git checkout in the home directory). For apps managed by a
<strong>systemd</strong> unit it shows live status and logs; it does not run the apps itself —
systemd does. Anything portend shows, you can also do from a shell.</p>

<h2>Status dots</h2>
<ul>
  <li><span class="dot">&#128994;</span> running — the unit is active</li>
  <li><span class="dot">&#128308;</span> stopped or failed — a unit exists but is not running</li>
  <li><span class="dot">&#9898;</span> batch / cron — no unit for this directory (e.g. a scheduled job); logs only</li>
</ul>

<h2>Unit names</h2>
<p>A directory maps to a unit name by lower-casing and turning <code>_</code> into <code>-</code>:
<code>App_Main</code> &rarr; <code>app-main</code>. Use that name in the commands below.</p>

<h2>Pull &amp; Restart</h2>
<p>The button runs, in the app's directory:</p>
<pre>git pull
sudo systemctl restart &lt;unit&gt;</pre>
<p>A batch/cron directory has no unit, so it runs <code>git pull</code> only.</p>

<h2>Commands you can run yourself</h2>
<p>Replace <code>&lt;unit&gt;</code> with the unit name (e.g. <code>app-main</code>).</p>
<p>Current state and a recent log tail:</p>
<pre>systemctl status &lt;unit&gt;</pre>
<p>Restart, stop, or start (a stopped unit stays down until started):</p>
<pre>sudo systemctl restart &lt;unit&gt;
sudo systemctl stop &lt;unit&gt;
sudo systemctl start &lt;unit&gt;</pre>
<p>Follow logs live:</p>
<pre>journalctl -u &lt;unit&gt; -f</pre>
<p>Recent history, or a specific time window:</p>
<pre>journalctl -u &lt;unit&gt; --since "1 hour ago"
journalctl -u &lt;unit&gt; --since "2026-01-01" --until "2026-01-02"</pre>
<p>List all units:</p>
<pre>systemctl list-units '*.service'</pre>

<h2>Restart behavior</h2>
<ul>
  <li>A unit auto-restarts after any unexpected exit — crash, kill, or out-of-memory.</li>
  <li><code>systemctl stop</code> is respected: the app stays down until you start it.</li>
  <li>Repeated fast failures put a unit in <code>failed</code>; clear it with
      <code>sudo systemctl reset-failed &lt;unit&gt;</code>, then start it.</li>
</ul>

<h2>Where logs live</h2>
<p>Unit output goes to the systemd journal (<code>journalctl -u &lt;unit&gt;</code>). Directories
without a unit log to files, which portend reads directly.</p>
</body>
</html>"""
    return html.replace("{BASE_URL}", BASE_URL)


@app.get("/help", response_class=HTMLResponse)
async def help_page(_: str = Depends(check_auth)):
    return render_help()


# --- API routes (AI / machine access) ---

@app.get("/api/apps")
async def api_apps(_: str = Depends(check_auth)):
    apps = [get_status(d) for d in discover_apps()]
    return apps


@app.get("/api/log", response_class=PlainTextResponse)
async def api_log(app: str, n: int = 200, level: str = "INFO", _: str = Depends(check_auth)):
    app_dir = HOME / app
    if not app_dir.exists():
        raise HTTPException(status_code=404)
    return get_log_lines(app_dir, n, min_level=level)


@app.post("/api/refresh")
async def api_refresh(request: Request, _: str = Depends(check_auth)):
    body = await request.json()
    app_name = body.get("app")
    if not app_name:
        raise HTTPException(status_code=400, detail="missing 'app' field")
    app_dir = HOME / app_name
    if not (app_dir / ".git").exists():
        raise HTTPException(status_code=404, detail=f"not a deployed app: {app_dir}")
    _pull_and_restart(app_dir)
    return {"status": "refresh started", "app": app_name}


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, app: Optional[str] = None, level: str = "INFO", _: str = Depends(check_auth)):
    apps = [get_status(d) for d in discover_apps()]
    selected = next((a for a in apps if a["name"] == app), None)
    if selected is None and apps:
        selected = apps[0]
    log = get_log_lines(Path(selected["path"]), min_level=level) if selected else ""
    return render_page(request, apps, selected, log, level)


@app.post("/refresh")
async def refresh(request: Request, _: str = Depends(check_auth)):
    form = await request.form()
    app_name = form.get("app")
    if not app_name:
        raise HTTPException(status_code=400)
    app_dir = HOME / app_name
    if not (app_dir / ".git").exists():
        raise HTTPException(status_code=404)
    _pull_and_restart(app_dir)
    return RedirectResponse(url=f"{BASE_URL}/?app={app_name}", status_code=303)


@app.get("/log", response_class=PlainTextResponse)
async def log(app: str, _: str = Depends(check_auth)):
    app_dir = HOME / app
    if not app_dir.exists():
        raise HTTPException(status_code=404)
    return get_log_lines(app_dir)
