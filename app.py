"""App Dashboard — 배포된 앱 통합 대시보드"""
import os
import json
import time
import ssl
import base64
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import docker

app = FastAPI(title="App Dashboard")

# Config
HARBOR_URL = os.getenv("HARBOR_URL", "https://techcs4899.mycafe24.com:8443")
HARBOR_AUTH = os.getenv("HARBOR_AUTH", "")  # base64 encoded user:pass
DOMAIN = os.getenv("DOMAIN", "techcs4899.mycafe24.com")
APP_PREFIX = os.getenv("APP_PREFIX", "app-")
NETWORK_NAME = os.getenv("NETWORK_NAME", "hermes-net")

# URL path overrides (app_name → actual nginx path)
URL_OVERRIDES = {
    "search-portal": "/search/",
    "dashboard": "/dashboard/",
    "test-harbor-pipeline": "/test-harbor-pipeline/",
}

# Docker client
docker_client = docker.DockerClient(base_url="unix:///var/run/docker.sock")

# SSL context for Harbor API
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

KST = timezone(timedelta(hours=9))


def harbor_api(path):
    """Call Harbor REST API"""
    try:
        req = Request(
            f"{HARBOR_URL}/api/v2.0{path}",
            headers={"Authorization": f"Basic {HARBOR_AUTH}"}
        )
        resp = urlopen(req, context=ssl_ctx, timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return None


def get_harbor_images():
    """Get all images from Harbor apps project"""
    repos = harbor_api("/projects/apps/repositories?page_size=50")
    if not repos:
        return {}
    
    images = {}
    for repo in repos:
        name = repo["name"].split("/")[-1]  # "apps/search-portal" → "search-portal"
        artifacts = harbor_api(f"/projects/apps/repositories/{name}/artifacts?page_size=1&sort=-push_time")
        if artifacts and len(artifacts) > 0:
            art = artifacts[0]
            tags = [t["name"] for t in art.get("tags", []) if t["name"] != "latest"]
            images[name] = {
                "size_mb": round(art["size"] / 1024 / 1024, 1),
                "push_time": art.get("push_time", ""),
                "digest": art.get("digest", "")[:19],
                "tags": tags,
                "tag_count": len(art.get("tags", [])),
            }
    return images


def get_running_apps():
    """Get running app containers"""
    apps = []
    try:
        containers = docker_client.containers.list(all=True)
    except Exception:
        return apps

    for c in containers:
        name = c.name
        if not name.startswith(APP_PREFIX):
            continue

        app_name = name[len(APP_PREFIX):]
        harbor_name = app_name  # default: same as app_name
        
        # Check image tag for actual harbor repo name
        image_str = c.image.tags[0] if c.image.tags else ""
        if "/apps/" in image_str:
            # e.g. "techcs4899.../apps/app-dashboard:latest" → "app-dashboard"
            harbor_name = image_str.split("/apps/")[-1].split(":")[0]
        
        # Container info
        status = c.status
        image = c.image.tags[0] if c.image.tags else str(c.image.short_id)
        
        # Uptime
        created = c.attrs.get("Created", "")
        uptime = ""
        if created:
            try:
                ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - ct
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                if hours > 24:
                    uptime = f"{hours // 24}일 {hours % 24}시간"
                elif hours > 0:
                    uptime = f"{hours}시간 {minutes}분"
                else:
                    uptime = f"{minutes}분"
            except:
                pass

        # Port info
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
        
        # URL path
        url_path = URL_OVERRIDES.get(app_name, f"/{app_name}/")

        apps.append({
            "name": app_name,
            "harbor_name": harbor_name,
            "container": name,
            "status": status,
            "image": image,
            "uptime": uptime,
            "url": f"https://{DOMAIN}{url_path}",
            "url_path": url_path,
        })

    return sorted(apps, key=lambda x: x["name"])


def get_system_info():
    """Get system-level container stats"""
    try:
        containers = docker_client.containers.list(all=True)
        running = sum(1 for c in containers if c.status == "running")
        total = len(containers)
        
        # Categorize
        apps = [c for c in containers if c.name.startswith(APP_PREFIX)]
        infra = [c for c in containers if not c.name.startswith(APP_PREFIX)]
        
        return {
            "total": total,
            "running": running,
            "stopped": total - running,
            "apps_count": len(apps),
            "infra_count": len(infra),
            "infra": [{"name": c.name, "status": c.status} for c in sorted(infra, key=lambda x: x.name)],
        }
    except:
        return {"total": 0, "running": 0, "stopped": 0, "apps_count": 0, "infra_count": 0, "infra": []}


@app.get("/api/dashboard")
def dashboard_api():
    """Main dashboard API"""
    apps = get_running_apps()
    images = get_harbor_images()
    system = get_system_info()

    # Merge harbor info into apps
    for a in apps:
        hname = a.get("harbor_name", a["name"])
        if hname in images:
            a["harbor"] = images[hname]
        else:
            a["harbor"] = None

    # Find images in Harbor but not deployed
    deployed_names = set()
    for a in apps:
        deployed_names.add(a["name"])
        deployed_names.add(a.get("harbor_name", a["name"]))
    undeployed = []
    for name, info in images.items():
        if name not in deployed_names:
            undeployed.append({"name": name, **info})

    return {
        "apps": apps,
        "undeployed": undeployed,
        "system": system,
        "harbor_url": HARBOR_URL,
        "domain": DOMAIN,
        "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


# ── Container Control API ──

PROTECTED_PREFIXES = ("hermes-", "harbor-", "github-runner", "nginx", "redis", "registry")


def _get_container(container_name: str):
    """Get container by name, with safety check"""
    # Only allow app- containers to be controlled
    if not container_name.startswith(APP_PREFIX):
        return None, {"error": "Only app containers can be controlled", "ok": False}
    # Block if somehow named after infra
    bare = container_name[len(APP_PREFIX):]
    for p in PROTECTED_PREFIXES:
        if bare.startswith(p):
            return None, {"error": "Protected container", "ok": False}
    try:
        return docker_client.containers.get(container_name), None
    except docker.errors.NotFound:
        return None, {"error": f"Container '{container_name}' not found", "ok": False}
    except Exception as e:
        return None, {"error": str(e), "ok": False}


@app.post("/api/containers/{container_name}/stop")
def container_stop(container_name: str):
    c, err = _get_container(container_name)
    if err:
        return err
    try:
        c.stop(timeout=10)
        return {"ok": True, "status": "stopped", "container": container_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/containers/{container_name}/start")
def container_start(container_name: str):
    c, err = _get_container(container_name)
    if err:
        return err
    try:
        c.start()
        return {"ok": True, "status": "running", "container": container_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/containers/{container_name}/restart")
def container_restart(container_name: str):
    c, err = _get_container(container_name)
    if err:
        return err
    try:
        c.restart(timeout=10)
        return {"ok": True, "status": "running", "container": container_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Logs API ──

@app.get("/api/containers/{container_name}/logs")
def container_logs(
    container_name: str,
    tail: int = Query(200, ge=10, le=5000),
    since: int = Query(0, ge=0),
):
    c, err = _get_container(container_name)
    if err:
        return err
    try:
        kwargs = {"tail": tail, "timestamps": True}
        if since > 0:
            kwargs["since"] = since
        logs = c.logs(**kwargs).decode("utf-8", errors="replace")
        return PlainTextResponse(logs)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Inspect API ──

@app.get("/api/containers/{container_name}/inspect")
def container_inspect(container_name: str):
    c, err = _get_container(container_name)
    if err:
        return err
    try:
        attrs = c.attrs
        net = attrs.get("NetworkSettings", {})
        state = attrs.get("State", {})
        config = attrs.get("Config", {})
        host_config = attrs.get("HostConfig", {})

        # Extract meaningful info
        networks = {}
        for name, detail in net.get("Networks", {}).items():
            networks[name] = {
                "ip": detail.get("IPAddress", ""),
                "gateway": detail.get("Gateway", ""),
                "mac": detail.get("MacAddress", ""),
            }

        mounts = []
        for m in attrs.get("Mounts", []):
            mounts.append({
                "type": m.get("Type", ""),
                "source": m.get("Source", ""),
                "dest": m.get("Destination", ""),
                "rw": m.get("RW", True),
            })

        ports = {}
        for port, bindings in (net.get("Ports") or {}).items():
            if bindings:
                ports[port] = [{"host_ip": b.get("HostIp", ""), "host_port": b.get("HostPort", "")} for b in bindings]
            else:
                ports[port] = None

        return {
            "ok": True,
            "id": attrs.get("Id", "")[:12],
            "name": attrs.get("Name", "").lstrip("/"),
            "image": config.get("Image", ""),
            "created": attrs.get("Created", ""),
            "state": {
                "status": state.get("Status", ""),
                "running": state.get("Running", False),
                "started_at": state.get("StartedAt", ""),
                "finished_at": state.get("FinishedAt", ""),
                "exit_code": state.get("ExitCode", 0),
                "pid": state.get("Pid", 0),
            },
            "env": [e for e in config.get("Env", []) if not any(s in e.upper() for s in ("SECRET", "PASSWORD", "TOKEN", "KEY", "AUTH"))],
            "cmd": config.get("Cmd"),
            "entrypoint": config.get("Entrypoint"),
            "working_dir": config.get("WorkingDir", ""),
            "networks": networks,
            "ports": ports,
            "mounts": mounts,
            "restart_policy": host_config.get("RestartPolicy", {}),
            "memory_limit": host_config.get("Memory", 0),
            "cpu_shares": host_config.get("CpuShares", 0),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Exec API ──

ALLOWED_COMMANDS = {
    "ps": ["ps", "aux"],
    "df": ["df", "-h"],
    "top": ["top", "-bn1"],
    "env": ["env"],
    "uname": ["uname", "-a"],
    "uptime": ["uptime"],
    "netstat": ["netstat", "-tlnp"],
    "ss": ["ss", "-tlnp"],
    "ls": ["ls", "-la", "/"],
    "whoami": ["whoami"],
    "cat-hosts": ["cat", "/etc/hosts"],
    "cat-resolv": ["cat", "/etc/resolv.conf"],
    "ip": ["ip", "addr"],
    "free": ["free", "-h"],
}


@app.post("/api/containers/{container_name}/exec")
def container_exec(container_name: str, cmd: str = Query(...)):
    c, err = _get_container(container_name)
    if err:
        return err

    if cmd not in ALLOWED_COMMANDS:
        return {"ok": False, "error": f"Command not allowed. Allowed: {', '.join(sorted(ALLOWED_COMMANDS.keys()))}"}

    try:
        result = c.exec_run(ALLOWED_COMMANDS[cmd], demux=True)
        stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
        stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
        return {
            "ok": True,
            "exit_code": result.exit_code,
            "stdout": stdout[-10000:],  # cap at 10KB
            "stderr": stderr[-5000:] if stderr else "",
            "cmd": " ".join(ALLOWED_COMMANDS[cmd]),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


app.mount("/static", StaticFiles(directory="static"), name="static")
