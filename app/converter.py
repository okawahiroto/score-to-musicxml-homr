"""homr CLI を呼び出し、散らばる出力ファイルを回収する層。

homr の CLI には出力先を指定するオプションが無く、書き出し先が 2 系統に分かれる。

  * 単ページの結果は入力ファイルと同じ階層 (``process_image`` の ``replace_extension``)
  * 複数ページ結合の結果はカレントディレクトリ (``run_homr`` の ``root_dir = os.getcwd()``)

そのためジョブごとの作業ディレクトリに入力を置き、そこを cwd として実行する。
これで両系統の出力が 1 か所に集まる。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# homr が受け付ける入力。これ以外は API 層で弾く。
ALLOWED_SUFFIXES = frozenset({".pdf", ".jpg", ".jpeg", ".png"})

# --coreml-encoder は起動時に 26〜60 秒のモデルコンパイルを伴うため、
# ページ数がこの値以上のときだけ元を取れる。
COREML_ENCODER_MIN_PAGES = 3

# homr が標準エラーに出す進捗ログ。
# "Finished parsing N staves" は 1 ページの途中で出るため、ページ完了と区別する。
_PROCESSING_RE = re.compile(r"^Processing (.+)$")
_FINISHED_RE = re.compile(r"^Finished (?!parsing\b)(.+)$")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# UI に出しても意味が無く、かつ利用者を不安にさせる行。
# E5RT は CoreML が一部ノードを CPU にフォールバックさせるときの通知で、
# 変換自体は成功する。RapidOCR の INFO はモデル読み込みの実況。
_NOISE_MARKERS = (
    "E5RT encountered an STL exception",
    "[RapidOCR]",
    "Inference Time Tromr:",
)

# "Writing XML [[...]]" は認識結果の全トークンを 1 行に吐くため数万文字になる。
_LOG_LINE_LIMIT = 300


def _clean_log_line(line: str, work_dir: Path | None = None) -> str | None:
    """UI 向けにログ 1 行を整える。捨てる行は None を返す。"""
    line = _ANSI_RE.sub("", line).rstrip()
    if not line.strip():
        return None
    if any(marker in line for marker in _NOISE_MARKERS):
        return None
    if work_dir is not None:
        # homr は絶対パスを吐くが、UI では作業ディレクトリ以下だけ見えれば十分。
        line = line.replace(str(work_dir) + os.sep, "")
    if len(line) > _LOG_LINE_LIMIT:
        return line[:_LOG_LINE_LIMIT] + f" …(残り {len(line) - _LOG_LINE_LIMIT} 文字を省略)"
    return line


class ConversionError(RuntimeError):
    """homr が結果を残さずに終了したときに送出する。"""


@dataclass
class ConversionResult:
    musicxml_path: Path
    teaser_paths: list[Path]
    page_count: int
    pages_succeeded: int
    merged: bool
    log: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.pages_succeeded < self.page_count


def count_pdf_pages(pdf_path: Path) -> int:
    """PDF のページ数を返す。homr と同じ pypdfium2 を使う。"""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def _expected_page_count(input_path: Path) -> int:
    if input_path.suffix.lower() == ".pdf":
        return count_pdf_pages(input_path)
    return 1


def _build_command(targets: list[Path], page_count: int, detect_title: bool) -> list[str]:
    # sys.executable と同じ環境の homr を確実に呼ぶ。
    cmd = [sys.executable, "-m", "homr.main"]
    if not detect_title:
        cmd.append("--no-title")
    if page_count >= COREML_ENCODER_MIN_PAGES:
        cmd.append("--coreml-encoder")
    cmd.extend(str(t) for t in targets)
    return cmd


def _stream_homr(
    cmd: list[str], cwd: Path, on_line: Callable[[str], None] | None
) -> Iterator[str]:
    """homr を起動し、標準エラーを 1 行ずつ流す。"""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    assert process.stderr is not None
    try:
        for raw in process.stderr:
            # 進捗判定は生の行に対して行い、表示用には整形した行を渡す。
            line = _ANSI_RE.sub("", raw.rstrip("\n"))
            cleaned = _clean_log_line(line, cwd)
            if cleaned is not None and on_line is not None:
                on_line(cleaned)
            yield line
    finally:
        process.stderr.close()
        process.wait()


def _collect_outputs(work_dir: Path) -> tuple[Path | None, list[Path]]:
    """作業ディレクトリから MusicXML と teaser 画像を回収する。

    出力名は入力種別とページ数で変わるため決め打ちせず走査する。

      * 画像 1 枚           -> ``<name>.musicxml``
      * PDF 1 ページ        -> ``<name>_0.musicxml`` (結合処理は走らない)
      * PDF 複数ページ      -> ``<name>_0_merged.musicxml`` (個別 xml は homr が削除)
    """
    xml_files = sorted(work_dir.glob("*.musicxml"))
    merged = [p for p in xml_files if p.stem.endswith("_merged")]
    if merged:
        musicxml = merged[0]
    elif xml_files:
        musicxml = xml_files[0]
    else:
        musicxml = None

    teasers = sorted(
        work_dir.glob("*_teaser.png"),
        key=lambda p: _page_sort_key(p.name),
    )
    return musicxml, teasers


def _page_sort_key(name: str) -> tuple[int, str]:
    """``score_10_teaser.png`` が ``score_2_teaser.png`` より後ろに来るようにする。"""
    match = re.search(r"_(\d+)_teaser\.png$", name)
    return (int(match.group(1)) if match else -1, name)


def convert(
    input_path: Path,
    work_dir: Path,
    detect_title: bool = True,
    on_line: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ConversionResult:
    """1 ファイルを MusicXML に変換する。

    homr の ``run_homr`` は 1 ページでも例外が出ると結合出力を作らずに戻る。
    その場合はページごとの再実行にフォールバックし、成功した分だけ返す。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    page_count = _expected_page_count(input_path)

    log: list[str] = []
    finished = 0

    def handle(line: str) -> None:
        """_stream_homr が整形済みの行を渡してくる。進捗判定もここで行う。"""
        nonlocal finished
        log.append(line)
        if on_line is not None:
            on_line(line)
        if _FINISHED_RE.match(line):
            finished += 1
            if on_progress is not None:
                on_progress(finished, page_count)

    cmd = _build_command([input_path], page_count, detect_title)
    for _ in _stream_homr(cmd, work_dir, handle):
        pass

    musicxml, teasers = _collect_outputs(work_dir)
    if musicxml is not None:
        return ConversionResult(
            musicxml_path=musicxml,
            teaser_paths=teasers,
            page_count=page_count,
            pages_succeeded=max(finished, 1),
            merged=musicxml.stem.endswith("_merged"),
            log=log,
        )

    if page_count > 1:
        result = _convert_pages_individually(
            input_path, work_dir, page_count, detect_title, handle, log
        )
        if result is not None:
            return result

    raise ConversionError(_diagnose(log))


def _convert_pages_individually(
    input_path: Path,
    work_dir: Path,
    page_count: int,
    detect_title: bool,
    handle: Callable[[str], None],
    log: list[str],
) -> ConversionResult | None:
    """一括処理が失敗したときに、ページ単位でやり直す。"""
    handle("[app] 一括処理に失敗したため、ページごとに再試行します")

    rendered = sorted(work_dir.glob(f"{input_path.stem}_*.png"), key=lambda p: _png_page_key(p.name))
    if not rendered:
        from homr.pdf_utils import render_pdf_to_image

        rendered = [Path(p) for p in render_pdf_to_image(str(input_path))]

    succeeded: list[Path] = []
    for page in rendered:
        if page.name.endswith("_teaser.png"):
            continue
        handle(f"[app] ページ {page.name} を単独で処理します")
        cmd = _build_command([page], 1, detect_title)
        for _ in _stream_homr(cmd, work_dir, handle):
            pass
        produced = page.with_suffix(".musicxml")
        if produced.exists():
            succeeded.append(produced)

    if not succeeded:
        return None

    teasers = sorted(work_dir.glob("*_teaser.png"), key=lambda p: _page_sort_key(p.name))

    if len(succeeded) == 1:
        return ConversionResult(
            musicxml_path=succeeded[0],
            teaser_paths=teasers,
            page_count=page_count,
            pages_succeeded=1,
            merged=False,
            log=log,
        )

    merged_path = _merge_musicxml(succeeded, work_dir, input_path.stem)
    return ConversionResult(
        musicxml_path=merged_path,
        teaser_paths=teasers,
        page_count=page_count,
        pages_succeeded=len(succeeded),
        merged=True,
        log=log,
    )


def _png_page_key(name: str) -> tuple[int, str]:
    match = re.search(r"_(\d+)\.png$", name)
    return (int(match.group(1)) if match else -1, name)


def _merge_musicxml(paths: list[Path], work_dir: Path, stem: str) -> Path:
    """成功したページだけを homr 自身の relieur で 1 本にまとめる。"""
    import xml.etree.ElementTree as ET

    from homr.relieur import process_concat

    merged, _, _ = process_concat([str(p) for p in paths])
    out = work_dir / f"{stem}_partial_merged.musicxml"
    ET.ElementTree(merged).write(str(out), encoding="UTF-8", xml_declaration=True)
    return out


def _diagnose(log: list[str]) -> str:
    """homr のログから、利用者が対処できる形のエラー文を作る。"""
    text = "\n".join(log)
    if "No noteheads found" in text:
        return "五線や音符を検出できませんでした。楽譜が写っているか、解像度が十分か確認してください。"
    if "No staffs found" in text:
        return "五線を検出できませんでした。傾きや影が強すぎる可能性があります。"
    if "The file format is not supported" in text:
        return "この画像形式は読み込めませんでした。JPG / PNG / PDF を使ってください。"
    tail = [line for line in log[-8:] if line.strip()]
    return "変換に失敗しました。\n" + "\n".join(tail) if tail else "変換に失敗しました。"


def reset_work_dir(work_dir: Path) -> None:
    if work_dir.exists():
        shutil.rmtree(work_dir)
