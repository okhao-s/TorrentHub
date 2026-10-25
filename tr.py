"""Transmission RPC 客户端（4.x）。"""
from __future__ import annotations

import time
from typing import Optional

import requests


class TrError(RuntimeError):
    pass


FIELDS = [
    "id", "hashString", "name", "status", "percentDone", "totalSize", "sizeWhenDone",
    "rateDownload", "rateUpload", "uploadRatio", "addedDate", "doneDate", "activityDate",
    "labels", "error", "errorString", "eta", "peersConnected", "isFinished",
    "downloadDir", "trackerStats", "secondsSeeding", "secondsDownloading",
]


class TrClient:
    def __init__(self, base: str, username: str = "", password: str = "", timeout: int = 20):
        base = (base or "").rstrip("/")
        if not base.endswith("/transmission/rpc"):
            base = base + "/transmission/rpc"
        self.url = base
        auth = (username, password) if username else None
        self.session = requests.Session()
        self.session.auth = auth
        self.session.headers.update({"User-Agent": "TorrentHub/0.1"})
        self.timeout = timeout
        self._sid = ""
        self._sid_at = 0.0

    def _call(self, method: str, arguments: Optional[dict] = None) -> dict:
        payload = {"method": method, "arguments": arguments or {}}
        for attempt in range(2):
            headers = {"X-Transmission-Session-Id": self._sid} if self._sid else {}
            resp = self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            if resp.status_code == 409:
                self._sid = resp.headers.get("X-Transmission-Session-Id", "")
                self._sid_at = time.time()
                continue
            if resp.status_code == 401:
                raise TrError("认证失败（401），检查账号密码")
            if resp.status_code >= 400:
                raise TrError(f"HTTP {resp.status_code}")
            data = resp.json()
            if data.get("result") != "success":
                raise TrError(str(data.get("result"))[:80])
            return data.get("arguments") or {}
        raise TrError("无法拿到 X-Transmission-Session-Id")

    def version(self) -> str:
        return self._call("session-get").get("version", "")

    def session_get(self) -> dict:
        return self._call("session-get")

    def session_stats(self) -> dict:
        return self._call("session-stats")

    def torrents(self) -> list[dict]:
        return self._call("torrent-get", {"fields": FIELDS}).get("torrents", []) or []

    def free_space(self, path: str = "") -> int | None:
        try:
            res = self._call("free-space", {"path": path} if path else {})
            return int(res.get("size-bytes")) if res.get("size-bytes") is not None else None
        except Exception:  # noqa: BLE001
            return None

    def set_limits(self, dl: int = 0, ul: int = 0) -> None:
        self._call("session-set", {
            "speed-limit-down-enabled": bool(dl), "speed-limit-down": int(dl),
            "speed-limit-up-enabled": bool(ul), "speed-limit-up": int(ul),
        })

    def set_torrent_limits(self, ids: list[int], dl: int | None = None, ul: int | None = None) -> None:
        args: dict = {"ids": [int(i) for i in ids if i is not None]}
        if dl is not None:
            args.update({"downloadLimited": bool(dl), "downloadLimit": int(dl)})
        if ul is not None:
            args.update({"uploadLimited": bool(ul), "uploadLimit": int(ul)})
        if len(args) > 1:
            self._call("torrent-set", args)

    def labels_for(self, ids: list[int]) -> dict[int, list[str]]:
        rows = self._call("torrent-get", {"ids": [int(i) for i in ids], "fields": ["id", "labels"]}).get("torrents") or []
        return {int(r["id"]): list(r.get("labels") or []) for r in rows}

    def set_labels(self, ids: list[int], labels: list[str]) -> None:
        self._call("torrent-set", {"ids": [int(i) for i in ids if i is not None], "labels": labels})

    def detail(self, tid: int) -> dict:
        res = self._call("torrent-get", {"ids": [int(tid)], "fields": [
            "id", "name", "hashString", "status", "totalSize", "sizeWhenDone", "percentDone",
            "rateDownload", "rateUpload", "uploadRatio", "addedDate", "doneDate", "downloadDir",
            "uploadedEver", "downloadedEver", "errorString", "comment", "creator", "dateCreated",
            "pieces", "pieceCount", "pieceSize", "isPrivate", "peersConnected", "peersGettingFromUs",
            "peersSendingToUs", "trackers", "trackerStats", "files", "labels", "secondsSeeding",
            "downloadLimited", "downloadLimit", "uploadLimited", "uploadLimit", "corruptEver",
        ]})
        rows = res.get("torrents") or []
        return rows[0] if rows else {}

    def add(self, urls: list[str], download_dir: str = "", labels: list[str] | None = None,
            paused: bool = False) -> dict:
        """逐个添加磁力/URL（TR 一次只能加一个）。"""
        out = {}
        for url in urls:
            args: dict = {"filename": url, "paused": bool(paused)}
            if download_dir:
                args["download-dir"] = download_dir
            if labels:
                args["labels"] = labels
            res = self._call("torrent-add", args)
            out[url[:60]] = "added" if res.get("torrent-added") else (
                "duplicate" if res.get("torrent-duplicate") else "unknown")
        return out

    def add_file(self, filename: str, blob: bytes, download_dir: str = "",
                 labels: list[str] | None = None, paused: bool = False) -> dict:
        import base64

        args: dict = {"metainfo": base64.b64encode(blob).decode(), "paused": bool(paused)}
        if download_dir:
            args["download-dir"] = download_dir
        if labels:
            args["labels"] = labels
        res = self._call("torrent-add", args)
        return {"file": filename, "result": "added" if res.get("torrent-added") else "duplicate"}

    def action(self, action: str, ids: list[int], delete_local: bool = False) -> None:
        ids = [int(i) for i in ids if i is not None]
        if not ids:
            return
        if action == "pause":
            self._call("torrent-stop", {"ids": ids})
        elif action == "resume":
            self._call("torrent-start", {"ids": ids})
        elif action == "recheck":
            self._call("torrent-verify", {"ids": ids})
        elif action == "delete":
            self._call("torrent-remove", {"ids": ids, "delete-local-data": bool(delete_local)})
        elif action == "reannounce":
            self._call("torrent-reannounce", {"ids": ids})
        elif action == "retry":
            self._call("torrent-start", {"ids": ids})
            self._call("torrent-reannounce", {"ids": ids})
        else:
            raise TrError(f"不支持的操作：{action}")
