"""TorrentHub —— qBittorrent / Transmission 多实例统一面板（看数据 + 控制）。"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import qb
import tr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("torrenthub")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
REFRESH_SECONDS = 5.0

# ---------------------------------------------------------------- 状态映射
QB_STATE = {
    "error": "error", "missingFiles": "error",
    "uploading": "seeding", "stalledUP": "seeding", "forcedUP": "seeding",
    "queuedUP": "queued", "checkingUP": "checking",
    "downloading": "downloading", "metaDL": "downloading", "forcedDL": "downloading",
    "stalledDL": "downloading", "allocating": "downloading", "queuedDL": "queued",
    "checkingDL": "checking", "checkingResumeData": "checking",
    "pausedDL": "paused", "pausedUP": "paused", "stoppedDL": "paused", "stoppedUP": "paused",
    "moving": "other", "unknown": "other",
}
TR_STATE = {0: "paused", 1: "checking", 2: "checking", 3: "downloading", 4: "downloading",
            5: "seeding", 6: "seeding"}

STATE_LABEL = {
    "downloading": "下载中", "seeding": "做种中", "paused": "已暂停",
    "queued": "排队", "checking": "校验中", "error": "错误", "other": "其他",
}


def _norm_qb(inst: dict, raw: dict) -> dict:
    state_raw = (raw.get("state") or "").strip()
    state = QB_STATE.get(state_raw, "other")
    if raw.get("amount_left") == 0 and state == "paused":
        state = "paused"
    return {
        "hash": (raw.get("hash") or "").lower(),
        "name": raw.get("name") or "",
        "size": int(raw.get("size") or 0),
        "progress": float(raw.get("progress") or 0),
        "dl": int(raw.get("dlspeed") or 0),
        "ul": int(raw.get("upspeed") or 0),
        "ratio": float(raw.get("ratio") or 0),
        "added": int(raw.get("added_on") or 0),
        "done": int(raw.get("completion_on") or 0) if (raw.get("completion_on") or 0) > 0 else 0,
        "labels": [t.strip() for t in (raw.get("tags") or "").split(",") if t.strip()],
        "category": raw.get("category") or "",
        "state": state,
        "state_raw": state_raw,
        "eta": int(raw.get("eta") or 0) if (raw.get("eta") or 0) > 0 else 0,
        "tracker": raw.get("tracker") or "",
        "seeding_time": int(raw.get("seeding_time") or 0),
        "error_msg": raw.get("state") or "",
        "native_id": raw.get("hash") or "",
    }


def _norm_tr(inst: dict, raw: dict) -> dict:
    status = raw.get("status")
    state = TR_STATE.get(status, "other")
    if raw.get("error"):
        state = "error"
    tracker = ""
    stats = raw.get("trackerStats") or []
    if stats:
        tracker = stats[0].get("announce") or stats[0].get("host") or ""
    labels = raw.get("labels") or []
    if isinstance(labels, str):
        labels = [t.strip() for t in labels.split(",") if t.strip()]
    return {
        "hash": (raw.get("hashString") or "").lower(),
        "name": raw.get("name") or "",
        "size": int(raw.get("sizeWhenDone") or raw.get("totalSize") or 0),
        "progress": float(raw.get("percentDone") or 0),
        "dl": int(raw.get("rateDownload") or 0),
        "ul": int(raw.get("rateUpload") or 0),
        "ratio": float(raw.get("uploadRatio") or 0),
        "added": int(raw.get("addedDate") or 0),
        "done": int(raw.get("doneDate") or 0),
        "labels": labels,
        "category": "",
        "state": state,
        "state_raw": str(status),
        "eta": int(raw.get("eta") or 0) if (raw.get("eta") or 0) > 0 else 0,
        "tracker": tracker,
        "seeding_time": int(raw.get("secondsSeeding") or 0),
        "error_msg": raw.get("errorString") or "",
        "native_id": raw.get("id"),
    }


# ---------------------------------------------------------------- 客户端与快照
_client_lock = threading.Lock()
_clients: dict[int, tuple[tuple, Any]] = {}

_meta_cache: dict[int, tuple[float, dict]] = {}
_meta_lock = threading.Lock()

_snap_lock = threading.Lock()
_snap: dict = {"at": 0, "instances": [], "items": [], "error": ""}


def webui_of(inst: dict) -> str:
    """下载器自己的 Web 界面地址（没填就按类型推导）。"""
    custom = (inst.get("webui") or "").strip()
    if custom:
        return custom
    base = (inst.get("url") or "").rstrip("/")
    if not base:
        return ""
    if inst.get("kind") == "tr":
        return base.replace("/transmission/rpc", "") + "/transmission/web/"
    return base


def instance_meta(inst: dict, client, ttl: float = 60.0) -> dict:
    """磁盘剩余 / 全局限速，按实例缓存 TTL 秒。"""
    now = time.time()
    with _meta_lock:
        hit = _meta_cache.get(inst["id"])
        if hit and now - hit[0] < ttl:
            return hit[1]
    meta: dict = {"free": None, "limits": {}}
    try:
        if inst["kind"] == "qb":
            meta["free"] = client.free_space()
            meta["limits"] = client.limits()
        else:
            sess = client.session_get()
            meta["free"] = sess.get("download-dir-free-space")
            meta["limits"] = {
                "dl": int(sess.get("speed-limit-down") or 0) if sess.get("speed-limit-down-enabled") else 0,
                "ul": int(sess.get("speed-limit-up") or 0) if sess.get("speed-limit-up-enabled") else 0,
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("实例 %s 元信息读取失败：%s", inst["name"], exc)
    with _meta_lock:
        _meta_cache[inst["id"]] = (now, meta)
    return meta


def get_client(inst: dict):
    sig = (inst["kind"], inst["url"], inst["username"], inst["password"])
    with _client_lock:
        hit = _clients.get(inst["id"])
        if hit and hit[0] == sig:
            return hit[1]
        client = (qb.QbClient(inst["url"], inst["username"], inst["password"])
                  if inst["kind"] == "qb" else
                  tr.TrClient(inst["url"], inst["username"], inst["password"]))
        _clients[inst["id"]] = (sig, client)
        return client


def _merge(items_by_inst: list[tuple[dict, dict, list[dict], dict]]) -> list[dict]:
    """按 info hash 合并多个实例的同一资源。"""
    groups: dict[str, dict] = {}
    for inst, meta, rows, _stats in items_by_inst:
        for row in rows:
            key = row["hash"] or f"{inst['id']}:{row['native_id']}"
            target = {
                "instance_id": inst["id"], "instance_name": inst["name"], "kind": inst["kind"],
                "native_id": row["native_id"], "state": row["state"], "progress": row["progress"],
                "dl": row["dl"], "ul": row["ul"], "ratio": row["ratio"],
                "labels": row["labels"],
            }
            group = groups.get(key)
            if not group:
                groups[key] = {
                    "key": key, "hash": row["hash"], "name": row["name"], "size": row["size"],
                    "progress": row["progress"], "dl": row["dl"], "ul": row["ul"],
                    "ratio": row["ratio"], "added": row["added"], "done": row["done"],
                    "labels": list(row["labels"]), "category": row["category"],
                    "state": row["state"], "eta": row["eta"], "tracker": row["tracker"],
                    "error_msg": row.get("error_msg") or "",
                    "instances": [target],
                }
            else:
                group["instances"].append(target)
                group["size"] = max(group["size"], row["size"])
                group["progress"] = max(group["progress"], row["progress"])
                group["dl"] += row["dl"]
                group["ul"] += row["ul"]
                group["ratio"] = max(group["ratio"], row["ratio"])
                group["added"] = min(x for x in (group["added"], row["added"]) if x) if (group["added"] or row["added"]) else 0
                group["done"] = max(group["done"], row["done"])
                for label in row["labels"]:
                    if label not in group["labels"]:
                        group["labels"].append(label)
                if not group["category"]:
                    group["category"] = row["category"]
                if not group["tracker"]:
                    group["tracker"] = row["tracker"]
                if not group["error_msg"] and row.get("error_msg"):
                    group["error_msg"] = row["error_msg"]
                # 状态优先级：错误 > 下载 > 校验 > 做种 > 排队 > 暂停
                order = {"error": 0, "downloading": 1, "checking": 2, "seeding": 3, "queued": 4, "paused": 5, "other": 6}
                if order.get(row["state"], 9) < order.get(group["state"], 9):
                    group["state"] = row["state"]
    for group in groups.values():
        states = [t["state"] for t in group["instances"]]
        if "error" in states:
            group["state"] = "error"
        group["state_label"] = STATE_LABEL.get(group["state"], group["state"])
        group["instance_names"] = [t["instance_name"] for t in group["instances"]]
    return list(groups.values())


def build_snapshot() -> dict:
    instances = db.list_instances(only_enabled=True)
    per_inst: list[tuple[dict, dict, list[dict], dict]] = []
    inst_status: list[dict] = []
    for inst in instances:
        started = time.time()
        try:
            client = get_client(inst)
            if inst["kind"] == "qb":
                rows_raw = client.torrents()
                transfer = client.transfer_info()
                version = client.version()
                rows = [_norm_qb(inst, r) for r in rows_raw]
                meta = instance_meta(inst, client)
                stats = {
                    "dl": int(transfer.get("dl_info_speed") or 0),
                    "ul": int(transfer.get("up_info_speed") or 0),
                    "free": meta.get("free"),
                    "limits": meta.get("limits") or {},
                }
            else:
                rows_raw = client.torrents()
                stats_raw = client.session_stats()
                sess = client.session_get()
                version = sess.get("version", "")
                rows = [_norm_tr(inst, r) for r in rows_raw]
                meta = instance_meta(inst, client)
                stats = {
                    "dl": int(stats_raw.get("downloadSpeed") or 0),
                    "ul": int(stats_raw.get("uploadSpeed") or 0),
                    "free": meta.get("free") if meta.get("free") is not None else sess.get("download-dir-free-space"),
                    "limits": meta.get("limits") or {},
                }
            per_inst.append((inst, stats, rows, stats))
            inst_status.append({
                "id": inst["id"], "name": inst["name"], "kind": inst["kind"], "url": inst["url"],
                "enabled": bool(inst["enabled"]), "webui": webui_of(inst), "online": True, "version": version,
                "count": len(rows), "dl": stats["dl"], "ul": stats["ul"], "free": stats.get("free"),
                "limits": stats.get("limits") or {},
                "error": "", "took_ms": int((time.time() - started) * 1000),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("实例 %s 读取失败：%s", inst["name"], exc)
            inst_status.append({
                "id": inst["id"], "name": inst["name"], "kind": inst["kind"], "url": inst["url"],
                "enabled": bool(inst["enabled"]), "webui": webui_of(inst), "online": False, "version": "",
                "count": 0, "dl": 0, "ul": 0, "free": None, "limits": {},
                "error": str(exc)[:160], "took_ms": int((time.time() - started) * 1000),
            })
    items = _merge(per_inst) if per_inst else []
    snap = {
        "at": time.time(),
        "instances": inst_status,
        "items": items,
        "counts": {},
    }
    counts: dict[str, int] = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    snap["counts"] = counts
    return snap


def _refresh_loop():
    global _snap
    while True:
        try:
            snap = build_snapshot()
            with _snap_lock:
                _snap = snap
        except Exception as exc:  # noqa: BLE001
            logger.exception("快照刷新失败：%s", exc)
            with _snap_lock:
                _snap = {**_snap, "error": str(exc)[:200]}
        time.sleep(REFRESH_SECONDS)


def _sample_loop():
    while True:
        time.sleep(300)
        try:
            snap = current_snapshot()
            items = snap.get("items") or []
            counts = snap.get("counts") or {}
            free = sum((i.get("free") or 0) for i in (snap.get("instances") or []))
            db.add_sample({
                "ts": int(time.time()), "total": len(items),
                "seeding": counts.get("seeding", 0), "error": counts.get("error", 0),
                "downloading": counts.get("downloading", 0),
                "dl_speed": sum(int(i.get("dl") or 0) for i in items),
                "ul_speed": sum(int(i.get("ul") or 0) for i in items),
                "free": free,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("采样失败：%s", exc)


def current_snapshot() -> dict:
    global _snap
    with _snap_lock:
        snap = _snap
    if not snap.get("at") or time.time() - snap["at"] > REFRESH_SECONDS * 4:
        snap = build_snapshot()
        with _snap_lock:
            _snap = snap
    return snap


# ---------------------------------------------------------------- API
LOGIN_HTML = """<!doctype html><meta charset=utf-8><title>TorrentHub · 需要登录</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;height:100vh;display:grid;place-items:center;background:
 radial-gradient(900px 500px at 20% -10%,rgba(122,162,255,.16),transparent 60%),#090b10;
 color:#eef2fa;font:14px/1.6 "PingFang SC",system-ui,sans-serif}
form{display:grid;gap:12px;min-width:280px;padding:24px;border-radius:18px;
 border:1px solid rgba(255,255,255,.1);background:rgba(20,24,33,.9)}
h1{margin:0;font-size:17px;letter-spacing:.04em}
p{margin:0;color:#7b8698;font-size:12px}
input,button{font:inherit;height:38px;border-radius:10px;border:1px solid rgba(255,255,255,.12);
 background:rgba(255,255,255,.04);color:#eef2fa;padding:0 12px}
button{background:linear-gradient(180deg,#83a9ff,#6d92f2);color:#08101d;font-weight:600;cursor:pointer;border:0}
.err{color:#ffb3b3;font-size:12px;display:none}
</style>
<form method="post" action="/api/login">
  <h1>TorrentHub</h1><p>请输入访问密码</p>
  <input name="password" type="password" autofocus placeholder="访问密码" required>
  <button type="submit">进入</button>
  <div class="err" id="e">密码错误</div>
</form>
<script>
const e = new URLSearchParams(location.search).get('e');
if (e) document.getElementById('e').style.display = 'block';
</script>"""


def _pwd() -> str:
    return (db.get_settings().get("access_password") or "").strip()


def _auth_token(pwd: str) -> str:
    return hashlib.sha256((pwd + "|torrenthub|v1").encode()).hexdigest()[:32]


def _authed(request: Request) -> bool:
    pwd = _pwd()
    if not pwd:
        return True
    return hmac.compare_digest(request.cookies.get("th_auth") or "", _auth_token(pwd))


app = FastAPI(title="TorrentHub")


class InstanceIn(BaseModel):
    id: int | None = None
    name: str = ""
    kind: str = "qb"
    url: str = ""
    username: str = ""
    password: str = ""
    enabled: bool = True


class ActionIn(BaseModel):
    action: str
    targets: list[dict] = []          # [{instance_id, native_id}]
    delete_files: bool = False


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if (not _pwd() or path.startswith("/static/") or path == "/favicon.ico"
            or path in ("/api/login", "/api/version")):
        return await call_next(request)
    if _authed(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "需要登录"}, status_code=401)
    return HTMLResponse(LOGIN_HTML, status_code=401)


@app.post("/api/login")
async def login(request: Request):
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        # 不依赖 python-multipart：自己解 urlencoded 就够了（登录框只有 password 一个字段）
        raw = (await request.body()).decode("utf-8", "ignore")
        pwd = (urllib.parse.parse_qs(raw).get("password", [""])[0] or "").strip()
        is_form = True
    else:
        body = await request.json()
        pwd = (body.get("password") or "").strip()
        is_form = False
    real = _pwd()
    ok = (not real) or hmac.compare_digest(pwd, real)
    if not ok:
        if is_form:
            return RedirectResponse("/?e=1", status_code=303)
        return JSONResponse({"ok": False, "detail": "密码错误"}, status_code=401)
    target = "/" if is_form else None
    resp = RedirectResponse(target, status_code=303) if is_form else JSONResponse({"ok": True})
    resp.set_cookie("th_auth", _auth_token(real), max_age=86400 * 30, httponly=True, samesite="lax")
    return resp


@app.get("/api/version")
def version():
    return {"version": os.environ.get("TH_VERSION", "dev")}


@app.get("/api/config")
def get_config():
    cfg = db.get_settings()
    return {"access_password_set": bool((cfg.get("access_password") or "").strip()),
            "add_rules": _rules()}


@app.post("/api/config")
def set_config(payload: dict):
    pwd = (payload.get("access_password") or "").strip()
    clear = bool(payload.get("clear_password"))
    patch = {}
    if "add_rules" in payload:
        rules = payload.get("add_rules") or []
        cleaned = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            pattern = str(rule.get("match") or "").strip()
            iid = int(rule.get("instance_id") or 0)
            if pattern and db.get_instance(iid):
                cleaned.append({"match": pattern, "instance_id": iid,
                                "savepath": str(rule.get("savepath") or "").strip(),
                                "category": str(rule.get("category") or "").strip()})
        patch["add_rules"] = json.dumps(cleaned, ensure_ascii=False)
        db.save_settings(patch)
        db.add_audit("web", "rules", target=f"{len(cleaned)} 条规则")
        return {"ok": True, "add_rules": cleaned, "access_password_set": bool(_pwd())}
    if "access_password" in payload or clear:
        patch["access_password"] = "" if clear else pwd
    db.save_settings(patch)
    resp = JSONResponse({"ok": True, "access_password_set": bool(patch["access_password"])})
    if clear or not patch["access_password"]:
        resp.delete_cookie("th_auth")
    return resp


class AddIn(BaseModel):
    instance_id: int = 0          # 0 = 按规则自动选择
    urls: list[str] = []
    name_hint: str = ""
    savepath: str = ""
    category: str = ""
    labels: str = ""
    paused: bool = False
    dry_run: bool = False


def _clean_urls(raw: list[str]) -> list[str]:
    out = []
    for line in raw:
        for piece in str(line or "").splitlines():
            piece = piece.strip()
            if piece and (piece.startswith("magnet:") or piece.startswith("http")):
                out.append(piece)
    return out


def _add_common(inst: dict, urls: list[str], savepath: str, category: str,
                labels: str, paused: bool, dry_run: bool, rule: dict | None = None) -> dict:
    urls = _clean_urls(urls)
    if not urls:
        raise HTTPException(status_code=400, detail="没有可添加的磁力或链接")
    labels_list = [x.strip() for x in labels.replace("，", ",").split(",") if x.strip()]
    rule = rule or {}
    savepath = savepath or rule.get("savepath", "")
    category = category or rule.get("category", "")
    if dry_run:
        return {"ok": True, "dry_run": True, "instance": inst["name"], "count": len(urls),
                "sample": urls[0][:80], "savepath": savepath, "paused": paused,
                "rule": rule.get("match", "")}
    client = get_client(inst)
    if inst["kind"] == "qb":
        client.add_urls(urls, savepath=savepath, category=category,
                        tags=",".join(labels_list), paused=paused)
        result = {"added": len(urls)}
    else:
        result = client.add(urls, download_dir=savepath, labels=labels_list, paused=paused)
    threading.Thread(target=_force_refresh, daemon=True).start()
    return {"ok": True, "instance": inst["name"], "count": len(urls), "result": result}


@app.post("/api/add")
def add_torrents(payload: AddIn):
    hint = payload.name_hint or magnet_name(next(iter(_clean_urls(payload.urls) or [""])))
    inst, rule = _pick_instance(hint, payload.instance_id)
    try:
        res = _add_common(inst, payload.urls, payload.savepath, payload.category,
                          payload.labels, payload.paused, payload.dry_run, rule)
        if not payload.dry_run:
            db.add_audit("web", "add", target=f"{inst['name']} × {res.get('count', 0)}",
                         detail=res.get("sample", "")[:120])
        return res
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        db.add_audit("web", "add", target=inst["name"], detail=str(exc)[:120], ok=False)
        raise HTTPException(status_code=502, detail=f"添加失败：{exc}") from exc


@app.post("/api/add_file")
async def add_torrent_file(request: Request):
    """上传 .torrent 文件：表单字段 instance_id / savepath / category / labels / paused + file。"""
    raw = await request.body()
    ctype = request.headers.get("content-type") or ""
    if "multipart/form-data" not in ctype:
        raise HTTPException(status_code=400, detail="需要 multipart/form-data")
    # 轻量解析：只为拿到 boundary 内的字段，避免额外依赖 python-multipart
    boundary = ctype.split("boundary=")[-1].strip().strip('"')
    parts = raw.split(("--" + boundary).encode())
    fields: dict[str, str] = {}
    file_name, file_blob = "", b""
    for part in parts:
        if b"\r\n\r\n" not in part:
            continue
        head, body = part.split(b"\r\n\r\n", 1)
        body = body.rstrip(b"\r\n-")
        head_text = head.decode("utf-8", "ignore")
        name_m = [seg for seg in head_text.split(";") if "name=" in seg]
        if not name_m:
            continue
        field = name_m[0].split("name=")[-1].strip().strip('"')
        if "filename=" in head_text:
            file_name = head_text.split("filename=")[-1].strip().strip('"')
            file_blob = body
        else:
            fields[field] = body.decode("utf-8", "ignore")
    if not file_blob:
        raise HTTPException(status_code=400, detail="没有收到种子文件")
    meta = torrent_meta(file_blob)
    inst, rule = _pick_instance(meta["name"], int(fields.get("instance_id") or 0))
    paused = (fields.get("paused") or "").lower() in ("1", "true", "on")
    fields["savepath"] = fields.get("savepath") or rule.get("savepath", "")
    fields["category"] = fields.get("category") or rule.get("category", "")
    try:
        client = get_client(inst)
        if inst["kind"] == "qb":
            client.add_file(file_name or "upload.torrent", file_blob,
                            savepath=fields.get("savepath", ""), category=fields.get("category", ""),
                            tags=fields.get("labels", ""), paused=paused)
            result = {"added": 1}
        else:
            labels = [x.strip() for x in (fields.get("labels") or "").replace("，", ",").split(",") if x.strip()]
            result = client.add_file(file_name or "upload.torrent", file_blob,
                                     download_dir=fields.get("savepath", ""), labels=labels, paused=paused)
        db.add_audit("web", "add_file", target=inst["name"], detail=f"{file_name} {meta['size']}")
        threading.Thread(target=_force_refresh, daemon=True).start()
        return {"ok": True, "instance": inst["name"], "count": 1, "result": result,
                "torrent": meta}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"添加失败：{exc}") from exc


def _tracker_host(url: str) -> str:
    if not url:
        return "未知"
    if "//" in url:
        return url.split("//", 1)[1].split("/")[0].lower()
    return url.lower()


@app.get("/api/errors_by_tracker")
def errors_by_tracker():
    snap = current_snapshot()
    buckets: dict[str, dict] = {}
    for item in snap["items"]:
        if item["state"] != "error":
            continue
        host = _tracker_host(item.get("tracker") or "")
        b = buckets.setdefault(host, {"tracker": host, "count": 0, "names": [],
                                      "instances": {}, "sample": item.get("error_msg") or ""})
        b["count"] += 1
        if len(b["names"]) < 3:
            b["names"].append(item["name"][:70])
        for t in item["instances"]:
            b["instances"][t["instance_name"]] = b["instances"].get(t["instance_name"], 0) + 1
    return {"total": sum(b["count"] for b in buckets.values()),
            "groups": sorted(buckets.values(), key=lambda x: -x["count"])}


@app.post("/api/retry_errors")
def retry_errors(dry_run: bool = False, tracker: str = "", instance_id: int | None = None):
    """重试错误任务；可按 tracker 域名或实例过滤。"""
    snap = current_snapshot()
    errors = [item for item in snap["items"] if item["state"] == "error"]
    if tracker:
        errors = [i for i in errors if _tracker_host(i.get("tracker") or "") == tracker.lower()]
    grouped: dict[int, list] = {}
    for item in errors:
        for target in item["instances"]:
            if instance_id is not None and target["instance_id"] != instance_id:
                continue
            grouped.setdefault(target["instance_id"], []).append(target["native_id"])
    if not grouped:
        return {"ok": True, "retried": 0, "by_instance": {}, "tracker": tracker}
    if dry_run:
        return {"ok": True, "dry_run": True, "retried": 0, "tracker": tracker,
                "plan": {db.get_instance(i)["name"]: len(v) for i, v in grouped.items() if db.get_instance(i)}}
    result = {}
    for iid, ids in grouped.items():
        inst = db.get_instance(iid)
        if not inst:
            continue
        try:
            client = get_client(inst)
            client.action("retry", ids)
            result[inst["name"]] = len(ids)
        except Exception as exc:  # noqa: BLE001
            result[inst["name"]] = f"失败：{str(exc)[:60]}"
    total = sum(len(v) for v in grouped.values())
    db.add_audit("web", "retry_errors", target=tracker or "全部错误",
                 detail=f"{total} 个任务，" + "，".join(f"{k}:{v}" for k, v in result.items()))
    threading.Thread(target=_force_refresh, daemon=True).start()
    return {"ok": True, "retried": total, "by_instance": result, "tracker": tracker}


@app.get("/api/history")
def history(hours: int = 24):
    return {"items": db.list_samples(hours=max(1, min(hours, 168)))}


@app.get("/api/audit")
def audit(limit: int = 100, q: str = ""):
    return {"items": db.list_audit(limit=limit, q=q)}


# ---------------- 限速 ----------------
class LimitIn(BaseModel):
    instance_id: int
    dl: int = 0        # KB/s，0 = 不限
    ul: int = 0
    targets: list[dict] = []   # 单任务限速时可选


@app.get("/api/limits")
def get_limits():
    snap = current_snapshot()
    out = []
    for inst in snap["instances"]:
        out.append({"instance_id": inst["id"], "name": inst["name"], "kind": inst["kind"],
                    "free": inst.get("free"), **(inst.get("limits") or {})})
    return {"items": out}


@app.post("/api/limits")
def set_limits(payload: LimitIn):
    inst = db.get_instance(payload.instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    try:
        client = get_client(inst)
        client.set_limits(dl=payload.dl, ul=payload.ul)
    except Exception as exc:  # noqa: BLE001
        db.add_audit("web", "limit", target=inst["name"], detail=str(exc)[:120], ok=False)
        raise HTTPException(status_code=502, detail=f"设置失败：{exc}") from exc
    with _meta_lock:
        _meta_cache.pop(inst["id"], None)
    db.add_audit("web", "limit", target=inst["name"], detail=f"下载 {payload.dl} KB/s，上传 {payload.ul} KB/s")
    return {"ok": True}


class TorrentLimitIn(BaseModel):
    targets: list[dict] = []
    dl: int | None = None
    ul: int | None = None


@app.post("/api/torrent_limits")
def set_torrent_limits(payload: TorrentLimitIn):
    grouped: dict[int, list] = {}
    for t in payload.targets:
        if t.get("instance_id") is None:
            continue
        grouped.setdefault(int(t["instance_id"]), []).append(t.get("native_id"))
    if not grouped:
        raise HTTPException(status_code=400, detail="没有选中任务")
    errs = []
    for iid, ids in grouped.items():
        inst = db.get_instance(iid)
        if not inst:
            continue
        try:
            get_client(inst).set_torrent_limits(ids, dl=payload.dl, ul=payload.ul)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{inst['name']}: {str(exc)[:60]}")
    db.add_audit("web", "torrent_limit", target=",".join(str(t.get("native_id")) for t in payload.targets)[:200],
                 detail=f"dl={payload.dl} ul={payload.ul}", ok=not errs)
    if errs:
        raise HTTPException(status_code=502, detail="；".join(errs))
    threading.Thread(target=_force_refresh, daemon=True).start()
    return {"ok": True}


# ---------------- 批量分类 / 标签 ----------------
class BulkIn(BaseModel):
    targets: list[dict] = []
    category: str | None = None
    add_labels: list[str] = []
    remove_labels: list[str] = []


@app.post("/api/bulk")
def bulk_edit(payload: BulkIn):
    grouped: dict[int, tuple[list, list]] = {}
    for t in payload.targets:
        if t.get("instance_id") is None:
            continue
        iid = int(t["instance_id"])
        qb_ids, tr_ids = grouped.get(iid, ([], []))
        inst = db.get_instance(iid)
        if not inst:
            continue
        (qb_ids if inst["kind"] == "qb" else tr_ids).append(t.get("native_id"))
        grouped[iid] = (qb_ids, tr_ids)
    if not grouped:
        raise HTTPException(status_code=400, detail="没有选中任务")
    errs, done = [], 0
    for iid, (qb_ids, tr_ids) in grouped.items():
        inst = db.get_instance(iid)
        client = get_client(inst)
        try:
            if inst["kind"] == "qb":
                if qb_ids and payload.category is not None:
                    client.set_category(qb_ids, payload.category)
                if qb_ids and payload.add_labels:
                    client.add_tags(qb_ids, payload.add_labels)
                if qb_ids and payload.remove_labels:
                    client.remove_tags(qb_ids, payload.remove_labels)
                done += len(qb_ids)
            else:
                if tr_ids and (payload.add_labels or payload.remove_labels or payload.category is not None):
                    current = client.labels_for(tr_ids)
                    for tid in tr_ids:
                        labels = [l for l in current.get(tid, []) if l not in payload.remove_labels]
                        for extra in payload.add_labels + ([payload.category] if payload.category else []):
                            if extra and extra not in labels:
                                labels.append(extra)
                        client.set_labels([tid], labels)
                done += len(tr_ids)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{inst['name']}: {str(exc)[:60]}")
    db.add_audit("web", "bulk", target=f"{done} 个任务",
                 detail=f"分类={payload.category} +标签={payload.add_labels} -标签={payload.remove_labels}",
                 ok=not errs)
    if errs:
        raise HTTPException(status_code=502, detail="；".join(errs))
    threading.Thread(target=_force_refresh, daemon=True).start()
    return {"ok": True, "count": done}


def _qb_status(word: str) -> str:
    w = (word or "").lower()
    if "working" in w or "success" in w:
        return "ok"
    if "not contacted" in w or "not working" in w or "error" in w:
        return "error"
    if "updating" in w:
        return "waiting"
    return "other"


@app.get("/api/detail")
def detail(key: str):
    snap = current_snapshot()
    item = next((i for i in snap["items"] if i["key"] == key), None)
    if not item:
        raise HTTPException(status_code=404, detail="任务不存在（快照已更新，请刷新列表）")
    out = []
    for target in item["instances"]:
        inst = db.get_instance(target["instance_id"])
        if not inst:
            continue
        block = {"instance_id": inst["id"], "instance_name": inst["name"], "kind": inst["kind"],
                 "native_id": target["native_id"], "online": True, "error": "",
                 "savepath": "", "added": 0, "comment": "", "creator": "", "private": None,
                 "peers": 0, "limits": {}, "trackers": [], "files": [], "files_total": 0}
        try:
            client = get_client(inst)
            if inst["kind"] == "qb":
                d = client.detail(item["hash"])
                info, props = d["info"], d["properties"]
                block.update({
                    "savepath": info.get("save_path") or "", "added": int(info.get("added_on") or 0),
                    "comment": info.get("comment") or (props.get("comment") or ""),
                    "creator": props.get("created_by") or "", "private": bool(props.get("is_private")),
                    "peers": int(info.get("num_complete") or 0) + int(info.get("num_incomplete") or 0),
                    "limits": {"dl": int(info.get("dl_limit") or 0), "ul": int(info.get("up_limit") or 0)},
                })
                for t in d["trackers"]:
                    if t.get("url", "").startswith("**"):
                        continue
                    block["trackers"].append({
                        "url": t.get("url") or "", "status": _qb_status(t.get("status")),
                        "msg": t.get("msg") or "", "peers": int(t.get("num_peers") or 0),
                        "seeds": int(t.get("num_seeds") or 0), "leeches": int(t.get("num_leeches") or 0),
                    })
                files = d["files"]
                block["files_total"] = len(files)
                block["files"] = [{"name": f.get("name") or "", "size": int(f.get("size") or 0),
                                   "progress": float(f.get("progress") or 0),
                                   "priority": int(f.get("priority") or 1)} for f in files[:300]]
            else:
                d = client.detail(int(target["native_id"]))
                block.update({
                    "savepath": d.get("downloadDir") or "", "added": int(d.get("addedDate") or 0),
                    "comment": d.get("comment") or "", "creator": d.get("creator") or "",
                    "private": bool(d.get("isPrivate")),
                    "peers": int(d.get("peersConnected") or 0),
                    "limits": {"dl": int(d.get("downloadLimit") or 0) if d.get("downloadLimited") else 0,
                               "ul": int(d.get("uploadLimit") or 0) if d.get("uploadLimited") else 0},
                })
                seen = set()
                for t in (d.get("trackers") or []):
                    url = t.get("announce") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    block["trackers"].append({"url": url, "status": "ok" if t.get("lastAnnounceSucceeded") else "error",
                                              "msg": t.get("lastAnnounceResult") or (d.get("errorString") if not t.get("lastAnnounceSucceeded") else "") or "",
                                              "peers": int(t.get("lastAnnouncePeerCount") or 0),
                                              "seeds": 0, "leeches": 0})
                files = d.get("files") or []
                block["files_total"] = len(files)
                block["files"] = [{"name": f.get("name") or "", "size": int(f.get("length") or 0),
                                   "progress": float(f.get("bytesCompleted") or 0) / max(1, int(f.get("length") or 1)),
                                   "priority": 1} for f in files[:300]]
        except Exception as exc:  # noqa: BLE001
            block["online"] = False
            block["error"] = str(exc)[:160]
        out.append(block)
    return {"key": key, "name": item["name"], "size": item["size"], "hash": item["hash"],
            "state": item["state"], "instances": out}


# ---------------- 添加规则路由 ----------------
def _bdecode(data: bytes, i: int = 0):
    ch = data[i:i + 1]
    if ch == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if ch == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            val, i = _bdecode(data, i)
            out.append(val)
        return out, i + 1
    if ch == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            key, i = _bdecode(data, i)
            val, i = _bdecode(data, i)
            out[key.decode("utf-8", "ignore") if isinstance(key, bytes) else key] = val
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def torrent_meta(blob: bytes) -> dict:
    try:
        meta, _ = _bdecode(blob)
        info = meta.get("info") or {}
        size = info.get("length")
        if size is None:
            size = sum(int(f.get("length") or 0) for f in (info.get("files") or []))
        return {"name": str(info.get("name") or ""), "size": int(size or 0)}
    except Exception:  # noqa: BLE001
        return {"name": "", "size": 0}


def magnet_name(url: str) -> str:
    if not url.startswith("magnet:"):
        return ""
    query = urllib.parse.urlparse(url).query
    return urllib.parse.parse_qs(query).get("dn", [""])[0]


def _rules() -> list[dict]:
    try:
        raw = db.get_settings().get("add_rules") or "[]"
        rules = json.loads(raw)
        return rules if isinstance(rules, list) else []
    except Exception:  # noqa: BLE001
        return []


def _pick_instance(name: str, requested: int) -> tuple[dict, dict]:
    """requested=0 → 按规则匹配名称，匹配不到用第一个启用实例。"""
    if requested:
        inst = db.get_instance(requested)
        if not inst:
            raise HTTPException(status_code=404, detail="实例不存在")
        return inst, {}
    rules = _rules()
    lowered = (name or "").lower()
    for rule in rules:
        pattern = str(rule.get("match") or "").strip()
        if not pattern:
            continue
        try:
            if re.search(pattern, lowered, re.I):
                inst = db.get_instance(int(rule.get("instance_id") or 0))
                if inst:
                    return inst, rule
        except re.error:
            if pattern.lower() in lowered:
                inst = db.get_instance(int(rule.get("instance_id") or 0))
                if inst:
                    return inst, rule
    enabled = db.list_instances(only_enabled=True)
    if not enabled:
        raise HTTPException(status_code=400, detail="没有可用的下载器实例")
    return enabled[0], {}


@app.get("/api/summary")
def summary():
    snap = current_snapshot()
    total_dl = sum(i.get("dl") or 0 for i in snap["instances"])
    total_ul = sum(i.get("ul") or 0 for i in snap["instances"])
    total_size = sum(item["size"] for item in snap["items"])
    return {
        "at": snap["at"],
        "instances": snap["instances"],
        "counts": snap.get("counts", {}),
        "state_labels": STATE_LABEL,
        "total": len(snap["items"]),
        "dl": total_dl,
        "ul": total_ul,
        "size": total_size,
        "refresh": REFRESH_SECONDS,
    }


@app.get("/api/torrents")
def torrents(page: int = 1, size: int = 50, q: str = "", state: str = "",
             instance: str = "", label: str = "", sort: str = "added", order: str = "desc"):
    snap = current_snapshot()
    items = snap["items"]

    if q:
        needle = q.casefold()
        items = [i for i in items if needle in (i["name"] or "").casefold()]
    if state:
        wanted = {s for s in state.split(",") if s}
        items = [i for i in items if i["state"] in wanted]
    if instance:
        ids = {int(x) for x in instance.split(",") if x.isdigit()}
        items = [i for i in items if any(t["instance_id"] in ids for t in i["instances"])]
    if label:
        needle = label.casefold()
        items = [i for i in items if any(needle in (l or "").casefold() for l in i["labels"])]

    keymap = {
        "name": lambda i: (i["name"] or "").casefold(),
        "size": lambda i: i["size"],
        "progress": lambda i: i["progress"],
        "dl": lambda i: i["dl"],
        "ul": lambda i: i["ul"],
        "ratio": lambda i: i["ratio"],
        "added": lambda i: i["added"],
        "state": lambda i: i["state"],
    }
    key = keymap.get(sort, keymap["added"])
    items = sorted(items, key=key, reverse=(order != "asc"))

    total = len(items)
    page = max(1, page)
    size = min(max(10, size), 200)
    start = (page - 1) * size
    return {
        "items": items[start:start + size],
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
        "at": snap["at"],
    }


@app.post("/api/action")
def do_action(req: ActionIn):
    if req.action not in ("pause", "resume", "delete", "recheck", "reannounce", "retry"):
        raise HTTPException(status_code=400, detail="不支持的操作")
    grouped: dict[int, list] = {}
    for target in req.targets:
        iid = target.get("instance_id")
        if iid is None:
            continue
        grouped.setdefault(int(iid), []).append(target.get("native_id"))
    if not grouped:
        raise HTTPException(status_code=400, detail="没有选中任何任务")

    results = []
    for iid, native_ids in grouped.items():
        inst = db.get_instance(iid)
        if not inst:
            results.append({"instance_id": iid, "ok": False, "error": "实例不存在"})
            continue
        try:
            client = get_client(inst)
            client.action(req.action, native_ids, delete_files=req.delete_files) \
                if inst["kind"] == "qb" else \
                client.action(req.action, native_ids, delete_local=req.delete_files)
            results.append({"instance_id": iid, "ok": True, "count": len(native_ids)})
        except Exception as exc:  # noqa: BLE001
            results.append({"instance_id": iid, "ok": False, "error": str(exc)[:160]})
    db.add_audit("web", req.action,
                 target=",".join(sorted({str(t.get("native_id")) for t in req.targets}))[:300],
                 detail=f"实例 {sorted(grouped)}，{sum(len(v) for v in grouped.values())} 个任务"
                        + ("，删除文件" if req.delete_files and req.action == "delete" else ""),
                 ok=all(r["ok"] for r in results))
    threading.Thread(target=lambda: _force_refresh(), daemon=True).start()
    return {"ok": all(r["ok"] for r in results), "results": results}


def _force_refresh():
    global _snap
    time.sleep(0.6)
    try:
        snap = build_snapshot()
        with _snap_lock:
            _snap = snap
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/instances")
def instances():
    snap = current_snapshot()
    status = {s["id"]: s for s in snap["instances"]}
    out = []
    for inst in db.list_instances():
        s = status.get(inst["id"], {})
        out.append({
            "id": inst["id"], "name": inst["name"], "kind": inst["kind"], "url": inst["url"],
            "username": inst["username"], "enabled": bool(inst["enabled"]),
            "webui": webui_of(inst),
            "online": s.get("online", False), "version": s.get("version", ""),
            "count": s.get("count", 0), "error": s.get("error", ""),
            "has_password": bool(inst.get("password")),
        })
    return {"items": out}


@app.post("/api/instances")
def save_instance(payload: InstanceIn):
    data = payload.model_dump()
    if not data.get("url"):
        raise HTTPException(status_code=400, detail="地址不能为空")
    if not data["url"].startswith(("http://", "https://")):
        data["url"] = "http://" + data["url"]
    inst = db.save_instance(data)
    with _client_lock:
        _clients.pop(inst["id"], None)
    return inst


@app.delete("/api/instances/{iid}")
def delete_instance(iid: int):
    db.delete_instance(iid)
    with _client_lock:
        _clients.pop(iid, None)
    return {"ok": True}


@app.post("/api/instances/{iid}/test")
def test_instance(iid: int, payload: InstanceIn | None = None):
    inst = db.get_instance(iid)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    if payload and payload.url:
        inst = {**inst, "url": payload.url, "username": payload.username or inst["username"],
                "password": payload.password or inst["password"], "kind": payload.kind or inst["kind"]}
        with _client_lock:
            _clients.pop(iid, None)
    try:
        base = (inst["url"] or "").rstrip("/")
        client = (qb.QbClient(base, inst["username"], inst["password"]) if inst["kind"] == "qb"
                  else tr.TrClient(base, inst["username"], inst["password"]))
        t0 = time.time()
        version = client.version()
        rows = client.torrents()
        return {"ok": True, "version": version, "count": len(rows),
                "took_ms": int((time.time() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)[:200]})


@app.get("/api/health")
def health():
    snap = current_snapshot()
    return {"ok": True, "at": snap.get("at", 0), "instances": len(snap.get("instances", [])),
            "torrents": len(snap.get("items", []))}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.on_event("startup")
def _startup():
    db.init_db()
    db.init_stats()
    threading.Thread(target=_refresh_loop, name="snapshot", daemon=True).start()
    threading.Thread(target=_sample_loop, name="sampler", daemon=True).start()
    logger.info("TorrentHub 启动完成，实例数 %s", len(db.list_instances()))
