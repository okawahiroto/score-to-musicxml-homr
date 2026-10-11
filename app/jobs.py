"""変換ジョブの状態管理。

1 ページあたり CPU で数分かかるため HTTP リクエスト内では処理しない。
ジョブをキューに積み、ワーカースレッドで直列に実行する。
ONNX 推論を並列に走らせるとメモリを食い潰すため、ワーカーは意図的に 1 本。
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app import converter

Status = Literal["queued", "running", "done", "error"]

# UI に返すログの保持行数。
LOG_TAIL = 400


@dataclass
class Job:
    id: str
    filename: str
    work_dir: Path
    input_path: Path
    detect_title: bool
    status: Status = "queued"
    page_count: int = 0
    pages_done: int = 0
    pages_succeeded: int = 0
    error: str | None = None
    musicxml_name: str | None = None
    teaser_names: list[str] = field(default_factory=list)
    partial: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = (
                (self.finished_at or datetime.now(timezone.utc)) - self.created_at
            ).total_seconds()
            return {
                "id": self.id,
                "filename": self.filename,
                "status": self.status,
                "page_count": self.page_count,
                "pages_done": self.pages_done,
                "pages_succeeded": self.pages_succeeded,
                "progress": self._progress(),
                "error": self.error,
                "partial": self.partial,
                "has_musicxml": self.musicxml_name is not None,
                "teaser_count": len(self.teaser_names),
                "elapsed_seconds": round(elapsed, 1),
                "log": list(self.log)[-40:],
            }

    def _progress(self) -> int:
        if self.status == "done":
            return 100
        if self.status == "error":
            return 100
        if self.page_count <= 0:
            return 5
        # 最後の 1 割は MusicXML の書き出しと回収のために残す。
        return min(95, int(self.pages_done / self.page_count * 90) + 5)


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="homr")

    def create(self, filename: str, data: bytes, detect_title: bool) -> Job:
        job_id = uuid.uuid4().hex[:12]
        work_dir = self.root / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        # アップロード名は使わず、ジョブ ID 由来の安全な名前で保存する。
        suffix = Path(filename).suffix.lower()
        input_path = work_dir / f"score{suffix}"
        input_path.write_bytes(data)

        job = Job(
            id=job_id,
            filename=filename,
            work_dir=work_dir,
            input_path=input_path,
            detect_title=detect_title,
        )
        with self._lock:
            self._jobs[job_id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        converter.reset_work_dir(job.work_dir)
        return True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, job: Job) -> None:
        def set_status(**kwargs: Any) -> None:
            with job._lock:
                for key, value in kwargs.items():
                    setattr(job, key, value)

        def on_line(line: str) -> None:
            with job._lock:
                job.log.append(line)

        def on_progress(done: int, total: int) -> None:
            set_status(pages_done=done, page_count=max(total, done))

        set_status(status="running")
        try:
            page_count = converter._expected_page_count(job.input_path)
            set_status(page_count=page_count)
            on_line(f"[app] {job.filename} を {page_count} ページとして処理します")

            result = converter.convert(
                input_path=job.input_path,
                work_dir=job.work_dir,
                detect_title=job.detect_title,
                on_line=on_line,
                on_progress=on_progress,
            )
            set_status(
                status="done",
                musicxml_name=result.musicxml_path.name,
                teaser_names=[p.name for p in result.teaser_paths],
                page_count=result.page_count,
                pages_done=result.page_count,
                pages_succeeded=result.pages_succeeded,
                partial=result.partial,
                finished_at=datetime.now(timezone.utc),
            )
        except converter.ConversionError as exc:
            set_status(
                status="error", error=str(exc), finished_at=datetime.now(timezone.utc)
            )
        except Exception as exc:  # noqa: BLE001 - UI に理由を出すため握る
            on_line(f"[app] 予期しないエラー: {exc!r}")
            set_status(
                status="error",
                error=f"予期しないエラーが発生しました: {exc}",
                finished_at=datetime.now(timezone.utc),
            )
