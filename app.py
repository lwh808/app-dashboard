"""App Dashboard — 배포된 앱 통합 대시보드"""
import os
import json
import time
import ssl
import base64
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import docker

app = FastAPI(title="App Dashboard")

# Config
HARBOR_URL = os.getenv("HARBOR_URL", "https://techcs4899.mycafe24.com:8443")
HARBOR_AUTH = os.getenv("HARBOR_AUTH", "")  # base64 encoded user:pass
DOMAIN = os.getenv("DOMAIN", "techcs4899.mycafe24.com")
APP_PREFIX = os.getenv("APP_PREFIX", "app-")
NETWORK_NAME = os.getenv("NETWORK_NAME", "hermes-net")

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
        
        # URL path (convention: app name)
        url_path = f"/{app_name}/"

        apps.append({
            "name": app_name,
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
        if a["name"] in images:
            a["harbor"] = images[a["name"]]
        else:
            a["harbor"] = None

    # Find images in Harbor but not deployed
    deployed_names = {a["name"] for a in apps}
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


app.mount("/static", StaticFiles(directory="static"), name="static")
