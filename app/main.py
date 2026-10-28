from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
LOG_DIR = DATA_DIR / "logs"
HLS_DIR = DATA_DIR / "streams"
LOG_DIR.mkdir(parents=True, exist_ok=True)
HLS_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_USER = os.getenv("ADMIN_USER", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password-now")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48)
RIGHTS_DEFAULT = os.getenv("RIGHTS_TEXT", "YOUSSEF")
FACEBOOK_RTMP_BASE = "rtmps://live-api-s.facebook.com:443/rtmp/"
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE_PATH = os.getenv("FFPROBE_PATH", "ffprobe")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
MAX_STREAMS = max(1, min(50, int(os.getenv("MAX_STREAMS", "10"))))
MAX_HLS_JOBS = max(1, min(50, int(os.getenv("MAX_HLS_JOBS", "5"))))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}

app = FastAPI(title="DARK STREAM WEB", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dark_stream_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/streams", StaticFiles(directory=HLS_DIR, check_dir=False), name="streams")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


@app.middleware("http")
async def hls_response_headers(request: Request, call_next):
    if request.url.path.startswith("/streams/") and request.method == "OPTIONS":
        response = Response(status_code=204)
    else:
        response = await call_next(request)
    if request.url.path.startswith("/streams/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Range, Origin, Accept, Content-Type"
        path_lower = request.url.path.lower()
        if path_lower.endswith(".m3u8"):
            response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif path_lower.endswith(".ts"):
            response.headers["Content-Type"] = "video/mp2t"
            response.headers["Cache-Control"] = "public, max-age=30"
    return response

STATE_LOCK = threading.RLock()
STREAMS: dict[str, dict[str, Any]] = {}
HLS_JOBS: dict[str, dict[str, Any]] = {}
LOGIN_ATTEMPTS: dict[str, tuple[int, float]] = {}

STREAM_MODES = {
    "copy": {"label": "Stream Copy · بدون إعادة ترميز", "short": "Copy", "description": "أقل استهلاك للمعالج، ينقل الترميزات كما هي."},
    "compatible": {"label": "H.264/AAC · توافق", "short": "H.264/AAC", "description": "إعادة ترميز متوازنة بدقة قصوى 480p."},
    "low": {"label": "اقتصادي · 360p", "short": "Low 360p", "description": "استهلاك أقل للمعالج والإنترنت."},
    "quality": {"label": "متوازن · حتى 720p", "short": "Quality 720p", "description": "جودة أعلى مع حمل أكبر من الوضع الاقتصادي."},
}


def normalize_rights(value: Any) -> str:
    value = str(value or "")
    value = re.sub(r"[^\w .@-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:60]


def is_mp4_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    return parsed.path.lower().endswith(".mp4")


def validate_http_url(raw: Any) -> str:
    value = str(raw or "").strip().replace("\ufeff", "")
    if not value or len(value) > 8192:
        raise ValueError("أدخل رابط مصدر صحيحًا لا يتجاوز 8192 حرفًا.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("المسموح فقط روابط HTTP أو HTTPS كاملة.")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError("الرابط يحتوي على محارف غير مسموحة.")
    return value


def validate_stream_name(raw: Any) -> str:
    name = re.sub(r"\s+", " ", str(raw or "").strip())
    if not name or len(name) > 80:
        raise ValueError("اسم البث مطلوب، وبحد أقصى 80 حرفًا.")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("اسم البث يحتوي على محارف غير مسموحة.")
    return name


def validate_stream_key(raw: Any) -> str:
    key = str(raw or "").strip()
    if not key or len(key) > 512 or re.search(r"\s|[\r\n\x00]", key):
        raise ValueError("أدخل مفتاح بث Facebook صالحًا بدون مسافات.")
    return key


def run_probe(source_url: str) -> dict[str, Any]:
    if not shutil.which(FFPROBE_PATH):
        raise RuntimeError("FFprobe غير مثبت على السيرفر. ثبّت FFmpeg/FFprobe أولًا.")
    try:
        result = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=format_name,duration",
             "-show_entries", "stream=codec_type,codec_name", "-of", "json",
             "-timeout", "10000000", source_url],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("انتهت مهلة فحص الرابط بعد 30 ثانية.") from exc
    if result.returncode != 0:
        message = (result.stderr or "تعذر قراءة رابط المصدر.").strip()[-1200:]
        raise RuntimeError(message)
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("تعذر تحليل نتيجة FFprobe.") from exc
    stream_list = data.get("streams") or []
    video = next((s for s in stream_list if s.get("codec_type") == "video"), {})
    audio = next((s for s in stream_list if s.get("codec_type") == "audio"), {})
    if not video and not audio:
        raise RuntimeError("لم يعثر FFprobe على صوت أو فيديو في الرابط.")
    return {
        "ok": True,
        "format": (data.get("format") or {}).get("format_name", "unknown"),
        "duration": (data.get("format") or {}).get("duration"),
        "video": video.get("codec_name", "غير موجود"),
        "audio": audio.get("codec_name", "غير موجود"),
    }


def log_path_for(name: str) -> Path:
    token = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
    return LOG_DIR / f"stream-{token}.log"


def build_rtmp_args(source_url: str, stream_key: str, mode: str, rights_text: str = "") -> list[str]:
    if mode not in STREAM_MODES:
        raise ValueError("وضع البث غير معروف.")
    target = FACEBOOK_RTMP_BASE + stream_key
    args: list[str] = []
    if is_mp4_url(source_url):
        args += ["-stream_loop", "-1"]
    args += [
        "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-reconnect", "1", "-reconnect_at_eof", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10", "-rw_timeout", "15000000", "-i", source_url,
        "-map", "0:v:0?", "-map", "0:a:0?",
    ]
    rights_text = normalize_rights(rights_text)
    if mode == "copy" and not rights_text:
        args += ["-c:v", "copy", "-c:a", "copy"]
    else:
        profiles = {
            "compatible": {"width": 854, "bitrate": "900k", "maxrate": "1100k", "bufsize": "1800k", "audio": "96k", "fps": 24, "threads": "2"},
            "low": {"width": 640, "bitrate": "500k", "maxrate": "650k", "bufsize": "1000k", "audio": "64k", "fps": 20, "threads": "1"},
            "quality": {"width": 1280, "bitrate": "2200k", "maxrate": "2600k", "bufsize": "4400k", "audio": "128k", "fps": 25, "threads": "2"},
            "copy": {"width": 854, "bitrate": "900k", "maxrate": "1100k", "bufsize": "1800k", "audio": "96k", "fps": 24, "threads": "2"},
        }
        profile = "compatible" if rights_text and mode == "copy" else mode
        p = profiles[profile]
        video_filter = f"scale=w='min({p['width']},iw)':h=-2:flags=fast_bilinear"
        if rights_text:
            video_filter += (f",drawtext=text='{rights_text}':x=(w-text_w)/2:y=h-text_h-24:fontsize=24:fontcolor=white:"
                             "borderw=2:bordercolor=black:box=1:boxcolor=black@0.45:boxborderw=6")
        gop = p["fps"] * 2
        args += [
            "-vf", video_filter, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-r", str(p["fps"]), "-g", str(gop), "-keyint_min", str(gop),
            "-sc_threshold", "0", "-b:v", p["bitrate"], "-maxrate", p["maxrate"], "-bufsize", p["bufsize"],
            "-threads", p["threads"], "-threads:v", p["threads"], "-c:a", "aac", "-b:a", p["audio"], "-ar", "44100",
        ]
    args += ["-flvflags", "no_duration_filesize", "-f", "flv", target]
    return args


def stream_worker(job: dict[str, Any]) -> None:
    name = job["name"]
    while True:
        with STATE_LOCK:
            if job.get("stop_requested") or STREAMS.get(name) is not job:
                if STREAMS.get(name) is job:
                    STREAMS.pop(name, None)
                return
            job["status"] = "reconnecting" if job["retry_count"] else "starting"
        proc: subprocess.Popen | None = None
        try:
            if not shutil.which(FFMPEG_PATH):
                raise RuntimeError("FFmpeg غير مثبت على السيرفر.")
            args = [FFMPEG_PATH, *build_rtmp_args(job["url"], job["key"], job["mode"], job["rights_text"])]
            with log_path_for(name).open("ab") as log_file:
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=log_file, close_fds=True, start_new_session=True)
            with STATE_LOCK:
                if job.get("stop_requested"):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                job["process"] = proc
                job["status"] = "running"
                job["last_started_at"] = time.time()
            return_code = proc.wait()
        except Exception as exc:  # Store operational error and retry while job is active.
            return_code = -1
            with STATE_LOCK:
                job["last_error"] = str(exc)[:1500]
                try:
                    with log_path_for(name).open("a", encoding="utf-8") as log_file:
                        log_file.write(f"\n[{datetime.now(timezone.utc).isoformat()}] {exc}\n")
                except OSError:
                    pass
        with STATE_LOCK:
            job["process"] = None
            if job.get("stop_requested") or STREAMS.get(name) is not job:
                if STREAMS.get(name) is job:
                    STREAMS.pop(name, None)
                return
            job["retry_count"] += 1
            job["last_return_code"] = return_code
            job["status"] = "reconnecting"
        # Back off to avoid tight loops when the source or RTMP key is unavailable.
        for _ in range(5):
            time.sleep(1)
            with STATE_LOCK:
                if job.get("stop_requested") or STREAMS.get(name) is not job:
                    if STREAMS.get(name) is job:
                        STREAMS.pop(name, None)
                    return


def build_hls_args(job: dict[str, Any]) -> list[str]:
    source_url = job["url"]
    args: list[str] = ["-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-threads", "0",
                       "-analyzeduration", "2000000", "-probesize", "2M", "-rw_timeout", "15000000",
                       "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                       "-fflags", "+genpts+discardcorrupt+nobuffer", "-i", source_url,
                       "-map", "0:v:0?", "-map", "0:a:0?"]
    rights = normalize_rights(job.get("rights_text") or RIGHTS_DEFAULT)
    if job.get("add_rights") and rights:
        vf = (f"drawtext=text='{rights}':x=(w-text_w)/2:y=h-text_h-20:fontsize=24:fontcolor=white:"
              "borderw=2:bordercolor=black:box=1:boxcolor=black@0.5:boxborderw=6")
        args += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-threads", "2",
                 "-pix_fmt", "yuv420p", "-r", "25", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                 "-crf", "27", "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    else:
        args += ["-c", "copy"]
    output_dir = Path(job["output_dir"])
    segment = output_dir / f"seg_{job['generation']}_%06d.ts"
    playlist = output_dir / "index.m3u8"
    args += ["-max_muxing_queue_size", "4096", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
             "-hls_delete_threshold", "2", "-hls_flags",
             "delete_segments+append_list+independent_segments+omit_endlist+temp_file+program_date_time",
             "-hls_start_number_source", "epoch", "-hls_segment_filename", str(segment), str(playlist)]
    return args


def hls_worker(job: dict[str, Any]) -> None:
    while True:
        with STATE_LOCK:
            if job.get("stop_requested") or HLS_JOBS.get(job["id"]) is not job:
                HLS_JOBS.pop(job["id"], None)
                return
            job["status"] = "starting" if job["retry_count"] == 0 else "reconnecting"
            job["generation"] = uuid.uuid4().hex[:8]
        proc: subprocess.Popen | None = None
        try:
            if not shutil.which(FFMPEG_PATH):
                raise RuntimeError("FFmpeg غير مثبت على السيرفر.")
            with (LOG_DIR / f"hls-{job['id']}.log").open("ab") as log_file:
                proc = subprocess.Popen([FFMPEG_PATH, *build_hls_args(job)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=log_file, close_fds=True, start_new_session=True)
            with STATE_LOCK:
                if job.get("stop_requested"):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                job["process"] = proc
                job["status"] = "running"
                job["last_started_at"] = time.time()
            return_code = proc.wait()
        except Exception as exc:
            return_code = -1
            job["last_error"] = str(exc)[:1500]
            try:
                with (LOG_DIR / f"hls-{job['id']}.log").open("a", encoding="utf-8") as log_file:
                    log_file.write(f"\n[{datetime.now(timezone.utc).isoformat()}] {exc}\n")
            except OSError:
                pass
        with STATE_LOCK:
            job["process"] = None
            if job.get("stop_requested") or HLS_JOBS.get(job["id"]) is not job:
                HLS_JOBS.pop(job["id"], None)
                return
            job["retry_count"] += 1
            job["last_return_code"] = return_code
            job["status"] = "reconnecting"
        for _ in range(4):
            time.sleep(1)
            with STATE_LOCK:
                if job.get("stop_requested") or HLS_JOBS.get(job["id"]) is not job:
                    HLS_JOBS.pop(job["id"], None)
                    return


def terminate_job(job: dict[str, Any]) -> None:
    job["stop_requested"] = True
    proc = job.get("process")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        def force_kill() -> None:
            time.sleep(3)
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        threading.Thread(target=force_kill, daemon=True).start()


def seconds_human(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(time.time() - seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def serialize_stream(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job["name"], "type": job["type"], "mode": job["mode"],
        "mode_label": STREAM_MODES.get(job["mode"], {}).get("short", job["mode"]),
        "rights_text": job.get("rights_text", ""), "status": job["status"],
        "retry_count": job["retry_count"], "started_at": job["started_at"],
        "uptime": seconds_human(job.get("started_at")), "last_error": job.get("last_error", ""),
    }


def serialize_hls(job: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
    base = PUBLIC_BASE_URL or (str(request.base_url).rstrip("/") if request else "")
    playlist_path = Path(job["output_dir"]) / "index.m3u8"
    try:
        playlist_exists = playlist_path.is_file() and playlist_path.stat().st_size > 0
    except OSError:
        playlist_exists = False
    status = "جاهز" if playlist_exists else job["status"]
    return {
        "id": job["id"], "status": status, "url": f"{base}/streams/{job['id']}/index.m3u8",
        "add_rights": job["add_rights"], "rights_text": job.get("rights_text", ""),
        "retry_count": job["retry_count"], "uptime": seconds_human(job.get("started_at")),
        "last_error": job.get("last_error", ""),
    }


class LoginBody(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=512)


class SoloBody(BaseModel):
    name: str = Field(max_length=80)
    key: str = Field(max_length=512)
    url: str = Field(max_length=8192)
    mode: str = "copy"
    add_rights: bool = False
    rights_text: str = ""


class GroupItem(BaseModel):
    name: str = Field(max_length=80)
    key: str = Field(max_length=512)
    url: str = Field(max_length=8192)


class GroupBody(BaseModel):
    streams: list[GroupItem]
    mode: str = "copy"


class UrlBody(BaseModel):
    url: str = Field(max_length=8192)


class HlsBody(BaseModel):
    url: str = Field(max_length=8192)
    add_rights: bool = False
    rights_text: str = ""


class NameBody(BaseModel):
    name: str = Field(max_length=80)


class HlsIdBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)


def require_auth(request: Request) -> None:
    if request.session.get("logged_in") is not True:
        raise HTTPException(status_code=401, detail="انتهت الجلسة. سجّل الدخول مجددًا.")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if request.session.get("logged_in") is True:
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("logged_in") is True:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": ""})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    if request.session.get("logged_in") is not True:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "rights_default": RIGHTS_DEFAULT,
        "max_streams": MAX_STREAMS,
        "max_hls_jobs": MAX_HLS_JOBS,
    })


@app.post("/api/login")
def api_login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "unknown"
    count, until = LOGIN_ATTEMPTS.get(ip, (0, 0))
    now = time.time()
    if until > now:
        raise HTTPException(status_code=429, detail="محاولات كثيرة. انتظر قليلًا ثم حاول مجددًا.")
    user_ok = secrets.compare_digest(body.username.encode(), ADMIN_USER.encode())
    pass_ok = secrets.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    if not (user_ok and pass_ok):
        count += 1
        LOGIN_ATTEMPTS[ip] = (count, now + 600 if count >= 8 else 0)
        raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة.")
    LOGIN_ATTEMPTS.pop(ip, None)
    request.session.clear()
    request.session["logged_in"] = True
    request.session["username"] = ADMIN_USER
    request.session["login_time"] = now
    return {"ok": True, "redirect": "/dashboard"}


@app.post("/api/logout")
def api_logout(request: Request):
    request.session.clear()
    return {"ok": True, "redirect": "/login"}


@app.get("/api/state")
def api_state(request: Request, _: None = Depends(require_auth)):
    with STATE_LOCK:
        streams = [serialize_stream(s) for s in STREAMS.values()]
        hls_jobs = [serialize_hls(j, request) for j in HLS_JOBS.values()]
    return {"ok": True, "streams": streams, "hls_jobs": hls_jobs,
            "limits": {"streams": MAX_STREAMS, "hls_jobs": MAX_HLS_JOBS},
            "ffmpeg_available": bool(shutil.which(FFMPEG_PATH)),
            "ffprobe_available": bool(shutil.which(FFPROBE_PATH))}


@app.post("/api/check-url")
def api_check_url(body: UrlBody, _: None = Depends(require_auth)):
    try:
        url = validate_http_url(body.url)
        probe = run_probe(url)
        return {"ok": True, "probe": probe}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/stream/start")
def api_stream_start(body: SoloBody, _: None = Depends(require_auth)):
    try:
        name = validate_stream_name(body.name)
        key = validate_stream_key(body.key)
        url = validate_http_url(body.url)
        if body.mode not in STREAM_MODES:
            raise ValueError("اختر طريقة بث صحيحة.")
        if not shutil.which(FFMPEG_PATH):
            raise RuntimeError("FFmpeg غير مثبت على السيرفر. استخدم Docker أو ثبّت FFmpeg.")
        rights = normalize_rights(body.rights_text) if body.add_rights else ""
        if body.add_rights and not rights:
            rights = normalize_rights(RIGHTS_DEFAULT)
        with STATE_LOCK:
            if len(STREAMS) >= MAX_STREAMS and name not in STREAMS:
                raise ValueError(f"وصلت إلى الحد الأقصى للبثوث ({MAX_STREAMS}).")
            if name in STREAMS:
                raise ValueError("يوجد بث بهذا الاسم بالفعل. أوقفه أو استخدم اسمًا آخر.")
        run_probe(url)
        job = {"name": name, "key": key, "url": url, "type": "SOLO+RIGHTS" if rights else "SOLO",
               "mode": "compatible" if rights and body.mode == "copy" else body.mode,
               "rights_text": rights, "status": "starting", "retry_count": 0,
               "started_at": time.time(), "last_started_at": None, "process": None,
               "stop_requested": False, "last_error": "", "last_return_code": None}
        with STATE_LOCK:
            if name in STREAMS:
                raise ValueError("يوجد بث بهذا الاسم بالفعل.")
            STREAMS[name] = job
        job["thread"] = threading.Thread(target=stream_worker, args=(job,), daemon=True, name=f"stream-{hashlib.md5(name.encode()).hexdigest()[:8]}")
        job["thread"].start()
        return {"ok": True, "message": f"بدأ تجهيز البث: {name}", "stream": serialize_stream(job)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/stream/group")
def api_stream_group(body: GroupBody, _: None = Depends(require_auth)):
    if not 1 <= len(body.streams) <= 20:
        raise HTTPException(status_code=400, detail="أدخل من بث واحد إلى 20 بثًا في المجموعة.")
    if body.mode not in STREAM_MODES:
        raise HTTPException(status_code=400, detail="اختر طريقة بث صحيحة.")
    prepared: list[dict[str, str]] = []
    try:
        if not shutil.which(FFMPEG_PATH):
            raise RuntimeError("FFmpeg غير مثبت على السيرفر. استخدم Docker أو ثبّت FFmpeg.")
        names: set[str] = set()
        for item in body.streams:
            name = validate_stream_name(item.name)
            if name in names:
                raise ValueError(f"اسم مكرر داخل المجموعة: {name}")
            names.add(name)
            prepared.append({"name": name, "key": validate_stream_key(item.key), "url": validate_http_url(item.url)})
        with STATE_LOCK:
            existing = set(STREAMS.keys())
            if existing.intersection(names):
                raise ValueError("هناك اسم بث مستخدم بالفعل في المجموعة.")
            if len(existing) + len(prepared) > MAX_STREAMS:
                raise ValueError(f"العدد يتجاوز حد السيرفر ({MAX_STREAMS} بثوث).")
        for item in prepared:
            run_probe(item["url"])
        jobs = []
        for item in prepared:
            job = {**item, "type": "GROUP", "mode": body.mode, "rights_text": "", "status": "starting",
                   "retry_count": 0, "started_at": time.time(), "last_started_at": None, "process": None,
                   "stop_requested": False, "last_error": "", "last_return_code": None}
            with STATE_LOCK:
                if job["name"] in STREAMS:
                    raise ValueError(f"اسم البث مستخدم بالفعل: {job['name']}")
                STREAMS[job["name"]] = job
            job["thread"] = threading.Thread(target=stream_worker, args=(job,), daemon=True,
                                             name=f"stream-{hashlib.md5(job['name'].encode()).hexdigest()[:8]}")
            job["thread"].start()
            jobs.append(serialize_stream(job))
        return {"ok": True, "message": f"بدأ تجهيز {len(jobs)} بثوث.", "streams": jobs}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/stream/stop")
def api_stream_stop(body: NameBody, _: None = Depends(require_auth)):
    name = str(body.name or "").strip()
    with STATE_LOCK:
        job = STREAMS.get(name)
        if not job:
            raise HTTPException(status_code=404, detail="لا يوجد بث بهذا الاسم.")
        terminate_job(job)
    return {"ok": True, "message": f"تم إرسال أمر إيقاف البث: {name}"}


@app.post("/api/stream/stop-all")
def api_stream_stop_all(_: None = Depends(require_auth)):
    with STATE_LOCK:
        jobs = list(STREAMS.values())
        for job in jobs:
            terminate_job(job)
    return {"ok": True, "message": f"تم إرسال أمر إيقاف {len(jobs)} بث."}


@app.get("/api/stream/logs")
def api_stream_logs(name: str, _: None = Depends(require_auth)):
    try:
        content = log_path_for(name).read_text(encoding="utf-8", errors="replace")[-12000:]
    except FileNotFoundError:
        content = "لا توجد سجلات لهذا البث بعد."
    return {"ok": True, "name": name, "logs": content}


@app.post("/api/hls/start")
def api_hls_start(body: HlsBody, request: Request, _: None = Depends(require_auth)):
    try:
        url = validate_http_url(body.url)
        if not shutil.which(FFMPEG_PATH):
            raise RuntimeError("FFmpeg غير مثبت على السيرفر. استخدم Docker أو ثبّت FFmpeg.")
        rights = normalize_rights(body.rights_text or RIGHTS_DEFAULT) if body.add_rights else ""
        with STATE_LOCK:
            if len(HLS_JOBS) >= MAX_HLS_JOBS:
                raise ValueError(f"وصلت إلى الحد الأقصى لروابط HLS ({MAX_HLS_JOBS}).")
        run_probe(url)
        with STATE_LOCK:
            if len(HLS_JOBS) >= MAX_HLS_JOBS:
                raise ValueError(f"وصلت إلى الحد الأقصى لروابط HLS ({MAX_HLS_JOBS}).")
        job_id = uuid.uuid4().hex[:12]
        output_dir = HLS_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=False)
        job = {"id": job_id, "url": url, "add_rights": bool(body.add_rights), "rights_text": rights or RIGHTS_DEFAULT,
               "output_dir": str(output_dir), "status": "starting", "retry_count": 0, "started_at": time.time(),
               "last_started_at": None, "process": None, "stop_requested": False, "last_error": "",
               "last_return_code": None, "generation": ""}
        with STATE_LOCK:
            HLS_JOBS[job_id] = job
        job["thread"] = threading.Thread(target=hls_worker, args=(job,), daemon=True, name=f"hls-{job_id}")
        job["thread"].start()
        return {"ok": True, "message": "بدأ إنشاء رابط HLS.", "hls_job": serialize_hls(job, request)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/hls/logs")
def api_hls_logs(id: str, _: None = Depends(require_auth)):
    if not re.fullmatch(r"[0-9a-f]{12}", id):
        raise HTTPException(status_code=400, detail="معرّف HLS غير صالح.")
    try:
        content = (LOG_DIR / f"hls-{id}.log").read_text(encoding="utf-8", errors="replace")[-12000:]
    except FileNotFoundError:
        content = "لا توجد سجلات لهذا الرابط بعد."
    return {"ok": True, "id": id, "logs": content}


@app.post("/api/hls/stop")
def api_hls_stop(body: HlsIdBody, _: None = Depends(require_auth)):
    with STATE_LOCK:
        job = HLS_JOBS.get(body.id)
        if not job:
            raise HTTPException(status_code=404, detail="رابط HLS غير موجود أو تم إيقافه.")
        terminate_job(job)
    # Remove generated files after the worker exits, with a fallback cleanup.
    def cleanup_later() -> None:
        for _ in range(50):
            with STATE_LOCK:
                active = body.id in HLS_JOBS
            if not active:
                break
            time.sleep(0.2)
        shutil.rmtree(HLS_DIR / body.id, ignore_errors=True)
    threading.Thread(target=cleanup_later, daemon=True).start()
    return {"ok": True, "message": "تم إرسال أمر إيقاف رابط HLS."}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "DARK STREAM WEB", "ffmpeg": bool(shutil.which(FFMPEG_PATH))}


@app.on_event("shutdown")
def shutdown_event() -> None:
    with STATE_LOCK:
        for job in list(STREAMS.values()) + list(HLS_JOBS.values()):
            terminate_job(job)

