from __future__ import annotations

import json
import os
import secrets as pysecrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from av_review.busy import vision_busy
from av_review.cli import _health
from av_review.constants import API_TOKEN_PATH, HOME, LOG_DIR, SECRETS
from av_review.util import now_iso, redact


def ensure_token() -> Path:
    SECRETS.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS, 0o700)
    if not API_TOKEN_PATH.exists():
        API_TOKEN_PATH.write_text(pysecrets.token_hex(24), encoding="utf-8")
        os.chmod(API_TOKEN_PATH, 0o600)
    return API_TOKEN_PATH


def _token_ok(header: str, expected: str) -> bool:
    if not header or not expected:
        return False
    h = header.strip()
    if h.lower().startswith("bearer "):
        h = h[7:].strip()
    return pysecrets.compare_digest(h, expected)


def _json(handler: BaseHTTPRequestHandler, code: int, obj: dict):
    raw = json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _body(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _bool(qs, body, *keys, default=False):
    for k in keys:
        if k in body:
            v = body[k]
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("1", "true", "yes", "on")
        if k in qs:
            v = (qs[k][0] if qs[k] else "")
            return str(v).lower() in ("1", "true", "yes", "on")
    return default


def _int(qs, body, key, default=None):
    if key in body and body[key] is not None:
        try:
            return int(body[key])
        except Exception:
            return default
    if key in qs and qs[key]:
        try:
            return int(qs[key][0])
        except Exception:
            return default
    return default


class Handler(BaseHTTPRequestHandler):
    expected_token = ""

    def log_message(self, fmt, *args):
        # Never log Authorization / tokens / keys
        msg = fmt % args
        sys.stderr.write("%s - %s\n" % (self.address_string(), redact(msg)))

    def _auth(self) -> bool:
        auth = self.headers.get("Authorization") or self.headers.get("X-AV-Review-Token") or ""
        if _token_ok(auth, self.expected_token):
            return True
        _json(self, 401, {"ok": False, "error": "unauthorized", "hint": "Authorization: Bearer <token in secrets/api.token, not in URL>"})
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/health", "/status"):
            # health is useful without auth on LAN, but still keep it authed to be safe? 
            # Task: health check. I'll allow /health without auth (no secrets in response),
            # other routes require token.
            _json(self, 200, _health())
            return
        if not self._auth():
            return
        _json(self, 404, {"ok": False, "error": "not found", "paths": ["/health", "POST /review", "POST /catalog", "POST /daily"]})

    def do_POST(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        body = _body(self)
        # never accept keys in query
        for bad in ("api_key", "key", "token", "authorization", "password", "secret"):
            if bad in qs or bad in body:
                _json(self, 400, {"ok": False, "error": "do not put keys in URL or JSON body"})
                return
        dry = _bool(qs, body, "dry", "dry_run", default=False)
        limit = _int(qs, body, "limit", default=0 if dry else None)
        if u.path == "/review":
            self._spawn_or_run("review", dry=dry, limit=limit, extra=body)
            return
        if u.path == "/catalog":
            judge = _bool(qs, body, "judge", default=False)
            self._spawn_or_run("catalog", dry=dry, limit=limit, judge=judge)
            return
        if u.path == "/daily":
            judge = _bool(qs, body, "judge", default=False)
            self._spawn_or_run("daily", dry=dry, limit=limit, judge=judge)
            return
        _json(self, 404, {"ok": False, "error": "not found"})

    def _spawn_or_run(self, kind: str, dry: bool, limit: int | None, judge: bool = False, extra: dict | None = None):
        extra = extra or {}
        exe = str(HOME / "bin" / "av_review")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if kind == "review" and not dry and (limit is None or limit != 0):
            busy = vision_busy()
            if busy.get("busy"):
                _json(self, 409, {"ok": False, "error": "vision_busy", "busy": busy,
                                  "hint": "wait for the in-flight scan, or call with dry=1 / limit=0"})
                return
        # dry / limit=0 / catalog without judge: run inline (no Magpie vision)
        inline = dry or limit == 0 or (kind == "catalog" and not judge)
        if inline:
            args = [kind]
            if kind in ("review", "daily"):
                if dry:
                    args.append("--dry-run")
                if limit is not None:
                    args += ["--limit", str(limit)]
                if extra.get("codes"):
                    codes = extra["codes"]
                    if isinstance(codes, list):
                        codes = ",".join(codes)
                    args += ["--codes", str(codes)]
                if extra.get("from_json") or extra.get("from-json"):
                    args += ["--from-json", str(extra.get("from_json") or extra.get("from-json"))]
            if kind == "catalog":
                if dry:
                    args.append("--dry-run")
                # judge is not inline-with-busy; already excluded unless judge False
            proc = subprocess.run(
                [exe, *args], capture_output=True, text=True, timeout=180,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            try:
                payload = json.loads(proc.stdout or "{}")
            except Exception:
                payload = {"stdout": (proc.stdout or "")[:4000]}
            _json(self, 200 if proc.returncode == 0 else 500, {
                "ok": proc.returncode == 0, "mode": "inline", "cmd": kind,
                "dry": dry, "limit": limit, "returncode": proc.returncode,
                "result": payload, "stderr": redact(proc.stderr or "")[-2000:],
                "generated_at": now_iso(),
            })
            return
        # heavy job: spawn and return
        log = LOG_DIR / f"api-{kind}-{os.getpid()}.log"
        args = [kind]
        if kind == "catalog" and judge:
            args.append("--judge")
        if kind == "daily":
            args.append("--skip-if-busy")
            if judge:
                args.append("--judge")
        if limit is not None:
            args += ["--limit", str(limit)]
        if extra.get("codes"):
            codes = extra["codes"]
            if isinstance(codes, list):
                codes = ",".join(codes)
            args += ["--codes", str(codes)]
        with open(log, "ab") as fh:
            proc = subprocess.Popen(
                [exe, *args], stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        _json(self, 202, {
            "ok": True, "mode": "spawned", "cmd": kind, "pid": proc.pid,
            "log": str(log), "generated_at": now_iso(),
            "note": "heavy job started; poll /health; results land in profile/ + agent-xfer",
        })


def serve(host: str, port: int):
    ensure_token()
    token = API_TOKEN_PATH.read_text().strip()
    Handler.expected_token = token
    httpd = ThreadingHTTPServer((host, port), Handler)
    # Do not print the token
    print(f"av_review API listening on http://{host}:{port}  (health: GET /health, auth header required for POST)", flush=True)
    print(f"token file: {API_TOKEN_PATH} (mode 600, do not put token in URL or logs)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("api stopped", flush=True)
