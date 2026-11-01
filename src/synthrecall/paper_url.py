from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

MAX_PDF_BYTES = 500 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
PDF_MAGIC_SCAN_BYTES = 1024
DOWNLOAD_CHUNK_BYTES = 256 * 1024
USER_AGENT = "ResearchWeave/0.1 paper URL import"


class PaperURLImportError(RuntimeError):
    pass


class PaperURLImportCancelled(PaperURLImportError):
    pass


@dataclass(frozen=True)
class ResolvedPaperPDF:
    path: Path
    source_url: str
    pdf_url: str


@dataclass(frozen=True)
class _PDFCandidate:
    url: str
    score: int


class _PaperPageParser(HTMLParser):
    """Collect PDF hints commonly exposed by publisher and repository pages."""

    PDF_META_NAMES: ClassVar[set[str]] = {
        "citation_pdf_url",
        "eprints.document_url",
        "pdf_url",
        "dc.identifier",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: list[_PDFCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): (value or "").strip() for key, value in attrs}
        if tag.casefold() == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            content = values.get("content", "")
            if name in self.PDF_META_NAMES and content:
                self._append(content, 100)
            return
        if tag.casefold() == "link":
            href = values.get("href", "")
            media_type = values.get("type", "").casefold()
            if href and (media_type == "application/pdf" or "pdf" in values.get("title", "").casefold()):
                self._append(href, 90)
            return
        if tag.casefold() != "a":
            return
        href = values.get("href", "")
        if not href:
            return
        lowered = unquote(href).casefold()
        score = 0
        if re.search(r"(?:\.pdf)(?:$|[?#])", lowered):
            score += 70
        if "/pdf/" in lowered or lowered.endswith("/pdf"):
            score += 35
        if "download" in lowered and "pdf" in lowered:
            score += 25
        if values.get("type", "").casefold() == "application/pdf":
            score += 70
        if score:
            self._append(href, score)

    def _append(self, value: str, score: int) -> None:
        url = urljoin(self.base_url, value)
        if urlsplit(url).scheme.casefold() in {"http", "https"}:
            self.candidates.append(_PDFCandidate(url=url, score=score))


class PaperURLImporter:
    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def download(
        self,
        source_url: str,
        output_dir: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> ResolvedPaperPDF:
        normalized_url = normalize_paper_url(source_url)
        output_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[_PDFCandidate] = []
        arxiv_url = arxiv_pdf_url(normalized_url)
        if arxiv_url:
            candidates.append(_PDFCandidate(arxiv_url, 200))
        candidates.append(_PDFCandidate(normalized_url, 150))

        page_html: str | None = None
        final_page_url = normalized_url
        errors: list[str] = []
        attempted: set[str] = set()

        while candidates:
            candidate = max(candidates, key=lambda item: item.score)
            candidates.remove(candidate)
            if candidate.url in attempted:
                continue
            attempted.add(candidate.url)
            if len(attempted) > 10:
                break
            self._check_cancelled(cancel_requested)
            try:
                result, final_url = self._fetch_candidate(
                    candidate.url,
                    output_dir,
                    cancel_requested,
                    allow_html=page_html is None,
                )
            except PaperURLImportCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 — 单候选失败不阻塞其余候选
                errors.append(f"{candidate.url}: {exc}")
                continue
            if isinstance(result, Path):
                return ResolvedPaperPDF(
                    path=result,
                    source_url=normalized_url,
                    pdf_url=final_url,
                )
            page_html = result
            final_page_url = final_url
            parser = _PaperPageParser(final_page_url)
            parser.feed(page_html)
            candidates.extend(parser.candidates)

        detail = "\n".join(errors[-3:])
        suffix = f"\n\n{detail}" if detail else ""
        raise PaperURLImportError(
            "未能从该链接获得有效 PDF。页面可能需要登录、使用脚本加载下载地址，"
            f"或没有公开 PDF 链接。{suffix}"
        )

    def _fetch_candidate(
        self,
        url: str,
        output_dir: Path,
        cancel_requested: Callable[[], bool] | None,
        *,
        allow_html: bool,
    ) -> tuple[Path | str, str]:
        timeout = httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".url-import-",
                suffix=".part",
                dir=output_dir,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with httpx.Client(
                    timeout=timeout,
                    follow_redirects=True,
                    trust_env=True,
                ) as client, client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5",
                    },
                ) as response:
                    response.raise_for_status()
                    final_url = str(response.url)
                    declared_size = _content_length(response.headers)
                    if declared_size > MAX_PDF_BYTES:
                        raise PaperURLImportError("远程文件超过 500 MB，已停止下载。")
                    content_type = response.headers.get("Content-Type", "").casefold()
                    downloaded = 0
                    prefix = bytearray()
                    html_bytes = bytearray()
                    is_pdf: bool | None = None
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        self._check_cancelled(cancel_requested)
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_PDF_BYTES:
                            raise PaperURLImportError("远程文件超过 500 MB，已停止下载。")
                        if len(prefix) < PDF_MAGIC_SCAN_BYTES:
                            missing = PDF_MAGIC_SCAN_BYTES - len(prefix)
                            prefix.extend(chunk[:missing])
                        if is_pdf is None:
                            if b"%PDF-" in bytes(prefix):
                                is_pdf = True
                            elif len(prefix) >= PDF_MAGIC_SCAN_BYTES or declared_size and downloaded == declared_size:
                                is_pdf = False
                        if is_pdf is True:
                            if html_bytes:
                                temporary.write(html_bytes)
                                html_bytes.clear()
                            temporary.write(chunk)
                        else:
                            if len(html_bytes) + len(chunk) > MAX_HTML_BYTES:
                                raise PaperURLImportError("论文页面超过 5 MB，无法安全分析。")
                            html_bytes.extend(chunk)
                    if is_pdf is None:
                        is_pdf = b"%PDF-" in bytes(prefix)
                    if is_pdf:
                        target = _unique_target_path(
                            output_dir,
                            _response_filename(response.headers, final_url),
                        )
                        temporary.flush()
                        # Windows does not allow replacing an open NamedTemporaryFile.
                        temporary.close()
                        temporary_path.replace(target)
                        temporary_path = None
                        return target, final_url
                    if not allow_html:
                        raise PaperURLImportError("候选下载地址返回的内容不是 PDF。")
                    if "html" not in content_type and not _looks_like_html(html_bytes):
                        raise PaperURLImportError("链接返回的内容既不是 PDF，也不是论文网页。")
                    encoding = response.encoding or "utf-8"
                    return html_bytes.decode(encoding, errors="replace"), final_url
        except (httpx.HTTPError, OSError) as exc:
            raise PaperURLImportError(f"下载请求失败：{exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _check_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
        if cancel_requested is not None and cancel_requested():
            raise PaperURLImportCancelled("链接导入已取消。")


def normalize_paper_url(value: str) -> str:
    compact = value.strip()
    if not compact:
        raise PaperURLImportError("请输入论文链接或在线 PDF 链接。")
    parsed = urlsplit(compact)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise PaperURLImportError("仅支持完整的 http:// 或 https:// 链接。")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, ""))


def arxiv_pdf_url(value: str) -> str | None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if host not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return None
    match = re.match(
        r"^/(?:abs|pdf|html)/(?P<identifier>(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?)",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return f"https://arxiv.org/pdf/{match.group('identifier')}"


def _content_length(headers: httpx.Headers) -> int:
    try:
        return int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0


def _looks_like_html(value: bytes) -> bool:
    prefix = value[:512].lstrip().casefold()
    return prefix.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"<head" in prefix


def _response_filename(headers: httpx.Headers, final_url: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    plain = re.search(r'filename="?([^";]+)', disposition, flags=re.IGNORECASE)
    name = unquote(encoded.group(1)) if encoded else (plain.group(1).strip() if plain else "")
    if not name:
        name = unquote(Path(urlsplit(final_url).path).name) or "downloaded-paper.pdf"
    if not name.casefold().endswith(".pdf"):
        name += ".pdf"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe or "downloaded-paper.pdf"


def _unique_target_path(output_dir: Path, filename: str) -> Path:
    target = output_dir / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 10_000):
        candidate = output_dir / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise PaperURLImportError("无法为下载的 PDF 分配唯一文件名。")
