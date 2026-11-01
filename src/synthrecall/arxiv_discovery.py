from __future__ import annotations

import json
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
from urllib.request import getproxies

import httpx

ARXIV_API_URLS = ("https://export.arxiv.org/api/query",)
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
MAX_CACHED_PAPERS = 200
MAX_ARXIV_PDF_BYTES = 500 * 1024 * 1024
_arxiv_request_lock = Lock()
_last_arxiv_request_at = 0.0


class ArxivRateLimitError(RuntimeError):
    pass


class ArxivDownloadCancelledError(RuntimeError):
    pass


@dataclass
class DiscoveryScore:
    overall: int
    novelty: float
    relevance: float
    reproducibility: float
    reason: str
    summary_points: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class ArxivPaper:
    arxiv_id: str
    title: str
    authors: list[str]
    summary: str
    published: str
    updated: str
    categories: list[str]
    primary_category: str
    entry_url: str
    pdf_url: str
    comment: str = ""
    journal_ref: str = ""
    doi: str = ""
    score: DiscoveryScore | None = None
    is_saved: bool = False
    is_hidden: bool = False
    is_bookmarked: bool = False
    local_pdf_path: str = ""

    @property
    def authors_text(self) -> str:
        if not self.authors:
            return "Unknown authors"
        if len(self.authors) <= 3:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:3]) + ", et al."

    @property
    def year(self) -> int | None:
        date_text = self.published or self.updated
        if len(date_text) >= 4 and date_text[:4].isdigit():
            return int(date_text[:4])
        return None


class ArxivClient:
    def __init__(self, timeout: float = 60, api_urls: Iterable[str] | None = None) -> None:
        self.timeout = timeout
        self.api_urls = tuple(api_urls or ARXIV_API_URLS)

    def search(
        self,
        query: str,
        max_results: int = 20,
        start: int = 0,
        sort_by: str = "submittedDate",
        sort_order: str = "descending",
    ) -> list[ArxivPaper]:
        params = {
            "search_query": normalize_arxiv_query(query),
            "start": str(max(0, start)),
            "max_results": str(max(1, min(max_results, 50))),
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }
        response = self._request_feed(params)
        return parse_arxiv_feed(response.text)

    def _request_feed(self, params: dict[str, str]) -> httpx.Response:
        timeout = httpx.Timeout(self.timeout, connect=min(8.0, self.timeout))
        failures: list[str] = []
        _wait_for_arxiv_request_slot()
        # Keep the original direct connection as the first path. Some proxy configurations time out on arXiv.
        request_modes = [False]
        if getproxies():
            request_modes.append(True)
        for trust_env in request_modes:
            mode_name = "系统代理" if trust_env else "直连"
            for api_url in self.api_urls:
                url = f"{api_url}?{urlencode(params)}"
                try:
                    with httpx.Client(timeout=timeout, trust_env=trust_env) as client:
                        response = client.get(
                            url,
                            headers={
                                "User-Agent": "ResearchWeave/0.1 arXiv Discovery",
                                "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                                "Accept-Encoding": "identity",
                            },
                        )
                        if response.status_code == 429:
                            raise ArxivRateLimitError(_rate_limit_message(response))
                        response.raise_for_status()
                        return response
                except ArxivRateLimitError:
                    raise
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    failures.append(f"{mode_name} {api_url}: {exc}")

        attempted = "；".join(failures) or "没有可用的 arXiv API 端点"
        raise RuntimeError(
            f"无法连接 arXiv（已尝试 {len(self.api_urls)} 个端点，每次最多等待 {self.timeout:.0f} 秒）。"
            f"\n\n{attempted}"
        )

    def download_pdf(
        self,
        paper: ArxivPaper,
        output_dir: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        return self.download_pdf_url(
            pdf_url=paper.pdf_url,
            title=paper.title,
            output_dir=output_dir,
            arxiv_id=paper.arxiv_id,
            cancel_requested=cancel_requested,
        )

    def download_pdf_url(
        self,
        pdf_url: str,
        title: str,
        output_dir: Path,
        arxiv_id: str = "",
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        exact_target = output_dir / f"{safe_filename(title)}.pdf"
        if exact_target.exists() and _is_pdf_file(exact_target):
            return exact_target
        target = unique_pdf_path(output_dir, safe_filename(title), arxiv_id)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".arxiv-download-",
                suffix=".part",
                dir=output_dir,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with httpx.Client(
                    timeout=120,
                    follow_redirects=True,
                    trust_env=False,
                ) as client, client.stream(
                    "GET",
                    pdf_url,
                    headers={"User-Agent": "ResearchWeave/0.1 arxiv discovery"},
                ) as response:
                    response.raise_for_status()
                    try:
                        declared_size = int(response.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        declared_size = 0
                    if declared_size > MAX_ARXIV_PDF_BYTES:
                        raise RuntimeError("arXiv PDF 超过 500 MB，已停止下载。")
                    downloaded = 0
                    header = bytearray()
                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                        if cancel_requested is not None and cancel_requested():
                            raise ArxivDownloadCancelledError("arXiv PDF 下载已取消。")
                        if not chunk:
                            continue
                        if len(header) < 5:
                            missing = 5 - len(header)
                            header.extend(chunk[:missing])
                            if len(header) == 5 and bytes(header) != b"%PDF-":
                                raise RuntimeError("arXiv 返回的内容不是有效 PDF。")
                        downloaded += len(chunk)
                        if downloaded > MAX_ARXIV_PDF_BYTES:
                            raise RuntimeError("arXiv PDF 超过 500 MB，已停止下载。")
                        temporary.write(chunk)
                    if bytes(header) != b"%PDF-":
                        raise RuntimeError("arXiv 返回的内容不是有效 PDF。")
            if temporary_path is None:
                raise RuntimeError("无法创建 arXiv PDF 临时文件。")
            temporary_path.replace(target)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return target


class DiscoveryCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.hidden_ids: set[str] = set()
        self.saved_ids: set[str] = set()
        self.bookmarked_ids: set[str] = set()
        self.local_pdf_paths: dict[str, str] = {}
        self.abstract_translations_zh: dict[str, str] = {}
        self.recent_papers: dict[str, dict] = {}
        self.last_successful_refresh = ""
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.hidden_ids = set(data.get("hidden_ids", []))
        self.saved_ids = set(data.get("saved_ids", []))
        self.bookmarked_ids = set(data.get("bookmarked_ids", []))
        self.local_pdf_paths = {
            str(key): str(value)
            for key, value in dict(data.get("local_pdf_paths", {})).items()
            if str(value)
        }
        self.abstract_translations_zh = {
            str(key): str(value)
            for key, value in dict(data.get("abstract_translations_zh", {})).items()
            if str(value).strip()
        }
        self.recent_papers = {
            str(key): value
            for key, value in dict(data.get("recent_papers", {})).items()
            if isinstance(value, dict)
        }
        self.last_successful_refresh = str(data.get("last_successful_refresh") or "")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "hidden_ids": sorted(self.hidden_ids),
            "saved_ids": sorted(self.saved_ids),
            "bookmarked_ids": sorted(self.bookmarked_ids),
            "local_pdf_paths": self.local_pdf_paths,
            "abstract_translations_zh": self.abstract_translations_zh,
            "recent_papers": self.recent_papers,
            "last_successful_refresh": self.last_successful_refresh,
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary_path.replace(self.path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def apply_state(self, paper: ArxivPaper) -> None:
        paper.is_hidden = paper.arxiv_id in self.hidden_ids
        paper.is_saved = paper.arxiv_id in self.saved_ids
        paper.is_bookmarked = paper.arxiv_id in self.bookmarked_ids
        paper.local_pdf_path = self.local_pdf_paths.get(paper.arxiv_id, "")

    def remember_papers(self, papers: Iterable[ArxivPaper]) -> None:
        for paper in papers:
            self.recent_papers[paper.arxiv_id] = paper_to_cache_dict(paper)
        if len(self.recent_papers) > MAX_CACHED_PAPERS:
            newest = sorted(
                self.recent_papers.items(),
                key=lambda item: str(item[1].get("published") or item[1].get("updated") or ""),
                reverse=True,
            )[:MAX_CACHED_PAPERS]
            self.recent_papers = dict(newest)
        self.last_successful_refresh = datetime.now(UTC).isoformat()
        self.save()

    def mark_hidden(self, paper: ArxivPaper) -> None:
        self.hidden_ids.add(paper.arxiv_id)
        self.save()

    def mark_saved(self, paper: ArxivPaper) -> None:
        self.saved_ids.add(paper.arxiv_id)
        self.save()

    def toggle_bookmark(self, paper: ArxivPaper) -> bool:
        if paper.arxiv_id in self.bookmarked_ids:
            self.bookmarked_ids.remove(paper.arxiv_id)
            paper.is_bookmarked = False
        else:
            self.bookmarked_ids.add(paper.arxiv_id)
            paper.is_bookmarked = True
        self.save()
        return paper.is_bookmarked

    def set_local_pdf(self, paper: ArxivPaper, path: Path) -> None:
        self.local_pdf_paths[paper.arxiv_id] = str(path)
        paper.local_pdf_path = str(path)
        self.save()

    def abstract_translation(self, paper: ArxivPaper) -> str:
        return self.abstract_translations_zh.get(paper.arxiv_id, "")

    def set_abstract_translation(self, paper: ArxivPaper, translation: str) -> None:
        self.abstract_translations_zh[paper.arxiv_id] = translation.strip()
        self.save()

    def recent_paper_list(self) -> list[ArxivPaper]:
        papers: list[ArxivPaper] = []
        for data in self.recent_papers.values():
            paper = paper_from_cache_dict(data)
            if paper is not None:
                self.apply_state(paper)
                papers.append(paper)
        return sorted(papers, key=lambda paper: paper.published, reverse=True)


class DiscoveryRecommender:
    def __init__(self, focus_terms: Iterable[str] | None = None) -> None:
        self.focus_terms = [term.strip() for term in focus_terms or default_focus_terms() if term.strip()]

    def score(self, paper: ArxivPaper) -> DiscoveryScore:
        corpus = f"{paper.title}\n{paper.summary}\n{' '.join(paper.categories)}".lower()
        matched: list[str] = []
        for term in self.focus_terms:
            tokens = [token for token in re.split(r"[^a-zA-Z0-9]+", term.lower()) if token]
            if tokens and all(token in corpus for token in tokens):
                matched.append(term)

        category_boost = 8 if any(cat.startswith(("cs.CV", "cs.LG", "stat.ML")) for cat in paper.categories) else 0
        novelty_hits = count_hits(
            corpus,
            [
                "new",
                "novel",
                "first",
                "state-of-the-art",
                "sota",
                "efficient",
                "scaling",
                "dynamic",
                "adaptive",
            ],
        )
        reproducibility_hits = count_hits(
            corpus,
            ["code", "github", "implementation", "benchmark", "ablation", "imagenet", "coco", "open-source"],
        )
        relevance = min(10.0, 4.8 + len(matched) * 1.15 + category_boost / 8)
        novelty = min(10.0, 5.0 + novelty_hits * 0.8)
        reproducibility = min(10.0, 4.5 + reproducibility_hits * 0.7)

        recency_bonus = 0
        published = parse_arxiv_date(paper.published)
        if published is not None:
            age_days = (datetime.now(UTC) - published).days
            if age_days <= 14:
                recency_bonus = 7
            elif age_days <= 60:
                recency_bonus = 4
            elif age_days <= 180:
                recency_bonus = 2

        match_bonus = min(18, len(matched) * 10)
        preferred_category_bonus = 5 if category_boost else 0
        overall = round(
            relevance * 5.2
            + novelty * 2.4
            + reproducibility * 2.0
            + match_bonus
            + preferred_category_bonus
            + recency_bonus
        )
        overall = max(35, min(98, overall))
        reason = build_reason(matched, paper, overall)
        return DiscoveryScore(
            overall=overall,
            novelty=round(novelty, 1),
            relevance=round(relevance, 1),
            reproducibility=round(reproducibility, 1),
            reason=reason,
            summary_points=build_summary_points(paper, matched),
            matched_terms=matched[:8],
        )


def default_focus_terms() -> list[str]:
    return [
        "vision transformer",
        "dynamic convolution",
        "low rank",
        "efficient visual recognition",
        "attention",
        "segmentation",
        "image classification",
        "reproducibility",
        "benchmark",
        "large kernel",
        "state space model",
    ]


def normalize_arxiv_query(query: str) -> str:
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        return "(all:vision AND all:transformer) OR (all:dynamic AND all:convolution) OR (all:low AND all:rank)"
    if ":" in cleaned:
        return cleaned
    if " OR " in cleaned.upper():
        parts = re.split(r"\s+OR\s+", cleaned, flags=re.IGNORECASE)
        normalized_parts = [normalize_plain_query(part) for part in parts if part.strip()]
        return " OR ".join(f"({part})" for part in normalized_parts if part)
    if " AND " in cleaned.upper():
        parts = re.split(r"\s+AND\s+", cleaned, flags=re.IGNORECASE)
        normalized_parts = [normalize_plain_query(part) for part in parts if part.strip()]
        return " AND ".join(f"({part})" for part in normalized_parts if part)
    return normalize_plain_query(cleaned)


def normalize_plain_query(query: str) -> str:
    tokens = [token for token in re.split(r"\s+", query.strip()) if token]
    return " AND ".join(f"all:{token}" for token in tokens)


def parse_arxiv_feed(xml_text: str) -> list[ArxivPaper]:
    root = ET.fromstring(xml_text)
    papers: list[ArxivPaper] = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        entry_id = text_of(entry, "atom:id")
        arxiv_id = entry_id.rstrip("/").split("/")[-1]
        title = compact_whitespace(text_of(entry, "atom:title"))
        summary = compact_whitespace(text_of(entry, "atom:summary"))
        published = text_of(entry, "atom:published")
        updated = text_of(entry, "atom:updated")
        authors = [
            compact_whitespace(text_of(author, "atom:name"))
            for author in entry.findall("atom:author", ARXIV_NS)
        ]
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ARXIV_NS)
            if category.attrib.get("term")
        ]
        primary = entry.find("arxiv:primary_category", {"arxiv": "http://arxiv.org/schemas/atom"})
        primary_category = primary.attrib.get("term", "") if primary is not None else (categories[0] if categories else "")
        pdf_url = ""
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        if not pdf_url and entry_id:
            pdf_url = entry_id.replace("/abs/", "/pdf/") + ".pdf"
        papers.append(
            ArxivPaper(
                arxiv_id=arxiv_id,
                title=title,
                authors=[author for author in authors if author],
                summary=summary,
                published=published[:10],
                updated=updated[:10],
                categories=categories,
                primary_category=primary_category,
                entry_url=entry_id,
                pdf_url=pdf_url,
                comment=arxiv_extension_text(entry, "comment"),
                journal_ref=arxiv_extension_text(entry, "journal_ref"),
                doi=arxiv_extension_text(entry, "doi"),
            )
        )
    return papers


def paper_to_cache_dict(paper: ArxivPaper) -> dict:
    data = asdict(paper)
    data.pop("score", None)
    return data


def paper_from_cache_dict(data: dict) -> ArxivPaper | None:
    try:
        return ArxivPaper(
            arxiv_id=str(data.get("arxiv_id") or ""),
            title=str(data.get("title") or "Untitled arXiv paper"),
            authors=[str(author) for author in data.get("authors", []) if str(author).strip()],
            summary=str(data.get("summary") or ""),
            published=str(data.get("published") or ""),
            updated=str(data.get("updated") or ""),
            categories=[str(category) for category in data.get("categories", []) if str(category).strip()],
            primary_category=str(data.get("primary_category") or ""),
            entry_url=str(data.get("entry_url") or ""),
            pdf_url=str(data.get("pdf_url") or ""),
            comment=str(data.get("comment") or ""),
            journal_ref=str(data.get("journal_ref") or ""),
            doi=str(data.get("doi") or ""),
            is_saved=bool(data.get("is_saved")),
            is_hidden=bool(data.get("is_hidden")),
            is_bookmarked=bool(data.get("is_bookmarked")),
            local_pdf_path=str(data.get("local_pdf_path") or ""),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _wait_for_arxiv_request_slot() -> None:
    global _last_arxiv_request_at
    with _arxiv_request_lock:
        remaining = ARXIV_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_arxiv_request_at)
        if remaining > 0:
            time.sleep(remaining)
        _last_arxiv_request_at = time.monotonic()


def _rate_limit_message(response: httpx.Response) -> str:
    retry_after = response.headers.get("Retry-After", "").strip()
    retry_seconds = _retry_after_seconds(retry_after)
    delay = _format_retry_delay(retry_seconds if retry_seconds is not None else 60)
    return (
        "arXiv 正在限制当前网络出口（HTTP 429）。"
        f"服务端建议约 {delay} 后重试；期间可继续查看缓存结果。"
    )


def _retry_after_seconds(value: str) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return max(1, int(value))
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    remaining = (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    return max(1, int(remaining + 0.999))


def _format_retry_delay(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} 秒"
    if seconds < 5400:
        return f"{max(1, (seconds + 59) // 60)} 分钟"
    return f"{max(1, (seconds + 3599) // 3600)} 小时"


def text_of(element: ET.Element, path: str) -> str:
    child = element.find(path, ARXIV_NS)
    return child.text.strip() if child is not None and child.text else ""


def arxiv_extension_text(entry: ET.Element, tag: str) -> str:
    ns = {"arxiv": "http://arxiv.org/schemas/atom"}
    child = entry.find(f"arxiv:{tag}", ns)
    return child.text.strip() if child is not None and child.text else ""


def compact_whitespace(value: str) -> str:
    return " ".join((value or "").split())


def count_hits(corpus: str, keywords: Iterable[str]) -> int:
    return sum(1 for keyword in keywords if keyword in corpus)


def parse_arxiv_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            # 10 位纯日期串无时区成分,必须显式附加 UTC
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        # Python 3.11 的 fromisoformat 已直接支持 "Z" 后缀
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_reason(matched: list[str], paper: ArxivPaper, overall: int) -> str:
    if matched:
        focus = "、".join(matched[:3])
        return f"命中你的关注方向：{focus}。摘要显示方法和实验线索较集中，适合加入候选阅读。"
    if overall >= 82:
        return "主题和近期视觉/机器学习研究关联较强，值得快速扫读确认方法价值。"
    return "与当前关注方向有一定交集，建议先浏览摘要和实验设置再决定是否入库。"


def build_summary_points(paper: ArxivPaper, matched: list[str]) -> list[str]:
    points = []
    if matched:
        points.append("与关注主题相关：" + "、".join(matched[:4]))
    if paper.primary_category:
        points.append(f"arXiv 领域：{paper.primary_category}")
    if paper.comment:
        points.append(f"作者备注：{paper.comment}")
    clipped = paper.summary
    if len(clipped) > 170:
        clipped = clipped[:169].rstrip() + "..."
    if clipped:
        points.append(clipped)
    return points[:4]


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:140] or "arxiv-paper"


def _is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def unique_pdf_path(output_dir: Path, title: str, arxiv_id: str = "") -> Path:
    base = safe_filename(title)
    target = output_dir / f"{base}.pdf"
    if not target.exists():
        return target
    suffix = safe_filename(arxiv_id) if arxiv_id else "copy"
    target = output_dir / f"{base}-{suffix}.pdf"
    index = 2
    while target.exists():
        target = output_dir / f"{base}-{suffix}-{index}.pdf"
        index += 1
    return target
