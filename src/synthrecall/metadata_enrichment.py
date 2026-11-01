"""元数据自动补全:通过 DOI(CrossRef)与 arXiv ID(arXiv API)回填文献字段。

只回填缺失字段,不覆盖用户已填写的内容。网络失败抛 RuntimeError,由调用方
转成友好提示。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class PaperMetadata:
    title: str = ""
    authors: str = ""  # 分号分隔(项目内部格式)
    year: int | None = None
    venue: str = ""
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""


def _clean_doi(doi: str) -> str:
    value = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
        if value.casefold().startswith(prefix):
            value = value[len(prefix):]
    return value.strip()


def _clean_arxiv_id(value: str) -> str:
    text = value.strip()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arXiv:"):
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix):]
    return text.strip()


def _format_crossref_authors(items) -> str:
    names = []
    for item in items or []:
        given = str(item.get("given") or "").strip()
        family = str(item.get("family") or "").strip()
        full = f"{given} {family}".strip()
        if full:
            names.append(full)
    return "; ".join(names)


def _first_title(container) -> str:
    titles = container.get("title") or []
    if isinstance(titles, list) and titles:
        return str(titles[0]).strip()
    return ""


def fetch_metadata(doi: str = "", arxiv_id: str = "", timeout: float = 20.0) -> PaperMetadata:
    """按优先级查询:DOI → CrossRef;arXiv ID → arXiv API。两者都缺则报错。"""
    clean_doi = _clean_doi(doi)
    clean_arxiv = _clean_arxiv_id(arxiv_id)
    if not clean_doi and not clean_arxiv:
        raise ValueError("请先填写 DOI 或 arXiv ID,再执行补全。")

    result = PaperMetadata(doi=clean_doi, arxiv_id=clean_arxiv)

    if clean_doi:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(
                    f"https://api.crossref.org/works/{clean_doi}",
                    headers={"User-Agent": "ResearchWeave/0.1 (metadata enrichment)"},
                )
                response.raise_for_status()
                message = response.json().get("message", {})
        except httpx.TimeoutException as exc:
            raise RuntimeError("CrossRef 请求超时,请检查网络后重试。") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                raise RuntimeError(f"CrossRef 未收录该 DOI:{clean_doi}") from exc
            raise RuntimeError(f"CrossRef 返回 HTTP {status}。") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接 CrossRef:{exc}") from exc

        result = PaperMetadata(
            title=_first_title(message),
            authors=_format_crossref_authors(message.get("author")),
            year=_crossref_year(message),
            venue=_crossref_venue(message),
            doi=str(message.get("DOI") or clean_doi),
            url=str(message.get("URL") or ""),
        )
        # 有 DOI 结果时若同时给了 arXiv ID,补 abstract 就够了,不重复请求
        return result

    # arXiv 分支
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                "https://export.arxiv.org/api/query",
                params={"id_list": clean_arxiv},
                headers={"User-Agent": "ResearchWeave/0.1"},
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise RuntimeError("arXiv 请求超时,请检查网络后重试(arXiv 可能限流,稍后再试)。") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"无法连接 arXiv:{exc}") from exc

    entry = _parse_arxiv_entry(response.text, clean_arxiv)
    if entry is None:
        raise RuntimeError(f"arXiv 未返回该 ID 的条目:{clean_arxiv}")
    return entry


def _crossref_year(message: dict) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _crossref_venue(message: dict) -> str:
    container = message.get("container-title") or []
    if isinstance(container, list) and container:
        return str(container[0]).strip()
    return str(message.get("publisher") or "").strip()


def _parse_arxiv_entry(xml_text: str, clean_arxiv: str) -> PaperMetadata | None:
    import re

    if "Err 404" in xml_text or "<entry>" not in xml_text:
        return None
    entry_match = re.search(r"<entry>(.*?)</entry>", xml_text, flags=re.DOTALL)
    if entry_match is None:
        return None
    entry = entry_match.group(1)

    def tag_text(tag: str) -> str:
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", entry, flags=re.DOTALL)
        return match.group(1).strip() if match else ""

    def strip_html(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value).strip()

    title = strip_html(tag_text("title"))
    abstract = strip_html(tag_text("summary"))
    published = tag_text("published")
    year = None
    year_match = re.match(r"(\d{4})", published)
    if year_match:
        year = int(year_match.group(1))
    authors = "; ".join(
        strip_html(name)
        for name in re.findall(r"<name>(.*?)</name>", entry, flags=re.DOTALL)
        if strip_html(name)
    )
    journal_ref = strip_html(tag_text("journal_ref"))
    doi_in_entry = tag_text("arxiv:doi") or ""
    return PaperMetadata(
        title=title,
        authors=authors,
        year=year,
        venue=journal_ref or f"arXiv {clean_arxiv}",
        abstract=abstract,
        arxiv_id=clean_arxiv,
        doi=doi_in_entry.strip(),
        url=f"https://arxiv.org/abs/{clean_arxiv}",
    )
