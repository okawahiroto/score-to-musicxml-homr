"""FastAPI アプリ本体。ローカル実行専用。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import converter
from app.jobs import JobStore

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR.parent / "data" / "jobs"

# 楽譜 1 ページの写真や PDF を想定した上限。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

store = JobStore(DATA_DIR)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    store.shutdown()


# 自動生成のAPIドキュメント(/docs 等)は外部CDNを読み込むため無効にする。
app = FastAPI(
    title="score-to-musicxml",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 認証を持たないローカル専用アプリなので、ブラウザ経由の攻撃を入口で止める。
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# multipart の境界やフィールド分の余裕を見込んだ、リクエスト全体の上限。
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


@app.middleware("http")
async def guard_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    """別サイトからの書き込み(CSRF)と巨大な本文を拒否し、応答にセキュリティヘッダーを付ける。

    curl 等 Origin の無い要求は通す。
    """
    if request.method in WRITE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "許可されていないオリジンです。"}, status_code=403)
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "リクエストが大きすぎます。"}, status_code=413)
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


# 最後に追加したものが最も外側で動く。Host 検証(DNS rebinding 対策)を最初に行う。
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict[str, str]:
    """起動スクリプトが、この番号で動いているのが本アプリだと見分けるための応答。"""
    return {"app": "score-to-musicxml-homr"}


@app.post("/api/jobs")
async def create_job(
    file: Annotated[UploadFile, File()],
    detect_title: Annotated[bool, Form()] = True,
) -> dict[str, str]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in converter.ALLOWED_SUFFIXES:
        allowed = " / ".join(sorted(s.lstrip(".").upper() for s in converter.ALLOWED_SUFFIXES))
        raise HTTPException(400, f"対応していない形式です。{allowed} を指定してください。")

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"ファイルが大きすぎます。上限は {MAX_UPLOAD_BYTES // (1024 * 1024)} MB です。"
        )
    data = await file.read()
    if not data:
        raise HTTPException(400, "ファイルが空です。")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"ファイルが大きすぎます。上限は {MAX_UPLOAD_BYTES // (1024 * 1024)} MB です。"
        )

    job = store.create(file.filename or f"score{suffix}", data, detect_title)
    return {"id": job.id}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return [job.snapshot() for job in store.list()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません。")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/musicxml")
def download_musicxml(job_id: str) -> FileResponse:
    job = store.get(job_id)
    if job is None or job.musicxml_name is None:
        raise HTTPException(404, "MusicXML がまだありません。")
    path = _safe_child(job.work_dir, job.musicxml_name)
    download_name = f"{Path(job.filename).stem}.musicxml"
    return FileResponse(path, media_type="application/vnd.recordare.musicxml+xml", filename=download_name)


@app.get("/api/jobs/{job_id}/teaser/{index}")
def get_teaser(job_id: str, index: int) -> FileResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません。")
    if not 0 <= index < len(job.teaser_names):
        raise HTTPException(404, "その確認画像はありません。")
    return FileResponse(_safe_child(job.work_dir, job.teaser_names[index]), media_type="image/png")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, bool]:
    if not store.delete(job_id):
        raise HTTPException(404, "ジョブが見つかりません。")
    return {"deleted": True}


def _safe_child(work_dir: Path, name: str) -> Path:
    """work_dir の外を指す名前を弾く。"""
    path = (work_dir / name).resolve()
    if not path.is_relative_to(work_dir.resolve()) or not path.is_file():
        raise HTTPException(404, "ファイルが見つかりません。")
    return path
