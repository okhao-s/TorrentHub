"""qBittorrent WebAPI v2 客户端（只做 TorrentHub 需要的最小集合）。"""
from __future__ import annotations

import time
from typing import Optional

import requests


class QbError(RuntimeError):
    pass


class QbClient:
    def __init__(self, base: str, username: str = "", password: str = "", timeout: int = 20):
        self.base = (base or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "TorrentHub/0.1", "Referer": self.base})
        self._sid_at = 0.0

    # ---------- 会话 ----------
    def login(self) -> None:
        if not self.username:
            return
        resp = self.session.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200 or "Ok" not in (resp.text or ""):
            raise QbError(f"登录失败（{resp.status_code}）：{resp.text[:60]}")
        self._sid_at = time.time()

    def _ensure(self):
        # 没有 SID 或超过 30 分钟就重新登录一次
        if self.username and (not self.session.cookies.get("SID") or time.time() - self._sid_at > 1800):
            self.login()

    def _get(self, path: str, **params):
        self._ensure()
        resp = self.session.get(f"{self.base}{path}", params=params or None, timeout=self.timeout)
        if resp.status_code == 403:            # SID 失效：重登一次再试
            self.login()
            resp = self.session.get(f"{self.base}{path}", params=params or None, timeout=self.timeout)
        if resp.status_code >= 400:
            raise QbError(f"HTTP {resp.status_code}：{path}")
        return resp

    def _post(self, path: str, data=None) -> None:
        self._ensure()
        resp = self.session.post(f"{self.base}{path}", data=data or {}, timeout=self.timeout)
        if resp.status_code == 403:
            self.login()
            resp = self.session.post(f"{self.base}{path}", data=data or {}, timeout=self.timeout)
        if resp.status_code >= 400:
            raise QbError(f"HTTP {resp.status_code}：{path}")

    # ---------- 查询 ----------
    def version(self) -> str:
        return self._get("/api/v2/app/version").text.strip()

    def transfer_info(self) -> dict:
        return self._get("/api/v2/transfer/info").json()

    def torrents(self) -> list[dict]:
        return self._get("/api/v2/torrents/info").json() or []

    def free_space(self) -> int | None:
        try:
            state = self._get("/api/v2/sync/maindata", rid=0).json().get("server_state") or {}
            val = state.get("free_space_on_disk")
            return int(val) if val is not None else None
        except Exception:  # noqa: BLE001
            return None

    def limits(self) -> dict:
        try:
            prefs = self._get("/api/v2/app/preferences").json()
            return {
                "dl": int(prefs.get("dl_limit") or 0), "ul": int(prefs.get("up_limit") or 0),
                "alt_dl": int(prefs.get("alt_dl_limit") or 0), "alt_ul": int(prefs.get("alt_up_limit") or 0),
                "alt_on": bool(prefs.get("alt_bw_limit_enabled")),
            }
        except Exception:  # noqa: BLE001
            return {}

    def set_limits(self, dl: int = 0, ul: int = 0) -> None:
        self._post("/api/v2/transfer/setDownloadLimit", {"limit": int(dl)})
        self._post("/api/v2/transfer/setUploadLimit", {"limit": int(ul)})

    def set_torrent_limits(self, hashes: list[str], dl: int | None = None, ul: int | None = None) -> None:
        joined = "|".join(h for h in hashes if h)
        if not joined:
            return
        if dl is not None:
            self._post("/api/v2/torrents/setDownloadLimit", {"hashes": joined, "limit": int(dl)})
        if ul is not None:
            self._post("/api/v2/torrents/setUploadLimit", {"hashes": joined, "limit": int(ul)})

    def set_category(self, hashes: list[str], category: str) -> None:
        joined = "|".join(h for h in hashes if h)
        if joined:
            self._post("/api/v2/torrents/setCategory", {"hashes": joined, "category": category})

    def add_tags(self, hashes: list[str], tags: list[str]) -> None:
        joined = "|".join(h for h in hashes if h)
        if joined and tags:
            self._post("/api/v2/torrents/addTags", {"hashes": joined, "tags": ",".join(tags)})

    def remove_tags(self, hashes: list[str], tags: list[str]) -> None:
        joined = "|".join(h for h in hashes if h)
        if joined and tags:
            self._post("/api/v2/torrents/removeTags", {"hashes": joined, "tags": ",".join(tags)})

    def detail(self, hash_: str) -> dict:
        info = self._get("/api/v2/torrents/info", hashes=hash_).json() or []
        row = info[0] if info else {}
        trackers = []
        try:
            trackers = self._get("/api/v2/torrents/trackers", hash=hash_).json() or []
        except Exception:  # noqa: BLE001
            pass
        files = []
        try:
            files = self._get("/api/v2/torrents/files", hash=hash_).json() or []
        except Exception:  # noqa: BLE001
            pass
        props = {}
        try:
            props = self._get("/api/v2/torrents/properties", hash=hash_).json() or {}
        except Exception:  # noqa: BLE001
            pass
        return {"info": row, "trackers": trackers, "files": files, "properties": props}

    def categories(self) -> dict:
        try:
            return self._get("/api/v2/torrents/categories").json() or {}
        except Exception:  # noqa: BLE001
            return {}

    def add_urls(self, urls: list[str], savepath: str = "", category: str = "",
                 tags: str = "", paused: bool = False) -> None:
        data = {"urls": "\n".join(urls)}
        if savepath:
            data["savepath"] = savepath
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        if paused:
            data["paused"] = "true"
        self._ensure()
        resp = self.session.post(f"{self.base}/api/v2/torrents/add", data=data, timeout=60)
        if resp.status_code == 403:
            self.login()
            resp = self.session.post(f"{self.base}/api/v2/torrents/add", data=data, timeout=60)
        if resp.status_code >= 400 or "Fail" in (resp.text or ""):
            raise QbError(f"添加失败：{resp.text[:80] or resp.status_code}")

    def add_file(self, filename: str, blob: bytes, savepath: str = "", category: str = "",
                 tags: str = "", paused: bool = False) -> None:
        form = {}
        if savepath:
            form["savepath"] = savepath
        if category:
            form["category"] = category
        if tags:
            form["tags"] = tags
        if paused:
            form["paused"] = "true"
        self._ensure()
        resp = self.session.post(
            f"{self.base}/api/v2/torrents/add",
            data=form,
            files={"torrents": (filename, blob, "application/x-bittorrent")},
            timeout=120,
        )
        if resp.status_code >= 400 or "Fail" in (resp.text or ""):
            raise QbError(f"添加失败：{resp.text[:80] or resp.status_code}")

    # ---------- 操作 ----------
    def action(self, action: str, hashes: list[str], delete_files: bool = False) -> None:
        joined = "|".join(h for h in hashes if h)
        if not joined:
            return
        if action == "pause":
            self._post("/api/v2/torrents/pause", {"hashes": joined})
        elif action == "resume":
            self._post("/api/v2/torrents/resume", {"hashes": joined})
        elif action == "recheck":
            self._post("/api/v2/torrents/recheck", {"hashes": joined})
        elif action == "delete":
            self._post("/api/v2/torrents/delete", {"hashes": joined, "deleteFiles": "true" if delete_files else "false"})
        elif action == "reannounce":
            self._post("/api/v2/torrents/reannounce", {"hashes": joined})
        elif action == "retry":
            # 错误任务重试：先强制开始，再重新汇报 tracker
            self._post("/api/v2/torrents/resume", {"hashes": joined})
            self._post("/api/v2/torrents/reannounce", {"hashes": joined})
        else:
            raise QbError(f"不支持的操作：{action}")
