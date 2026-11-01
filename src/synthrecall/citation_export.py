"""引用导出:把文献库导出为 BibTeX / RIS 文件。

BibTeX 细节:
- 有 DOI 或 URL 的会议论文(venue 含 conference/proceedings/conf.)输出
  @inproceedings,其余 @article;entry key 基于作者+年份+标题并保证同批唯一。
- LaTeX 特殊字符转义:& % # _ { } ~ ^ \\
- 作者分号分隔(项目内部格式)转为 " and "。

RIS 细节:
- TY 按 venue 推断(JOUR/CONF);ER 结尾; authors 分号 → 每行一个 AU。
"""
from __future__ import annotations

import re
from pathlib import Path

from synthrecall.models import Paper

_TEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_bibtex(value: str) -> str:
    result = []
    for char in value:
        result.append(_TEX_SPECIAL.get(char, char))
    return "".join(result)


def _bibtex_entry_type(paper: Paper) -> str:
    venue = (paper.venue or "").casefold()
    if re.search(r"conference|proceedings|\bconf\b|cvpr|iccv|eccv|neurips|icml|iclr|aaai|ijcai|siggraph|kdd|www|emnlp|acl\b", venue):
        return "inproceedings"
    return "article"


def _booktitle_or_journal(paper: Paper) -> str:
    return "booktitle" if _bibtex_entry_type(paper) == "inproceedings" else "journal"


def _unique_cite_key(paper: Paper, used: set[str]) -> str:
    first_author = (paper.authors or "paper").split(";", 1)[0].split(",", 1)[0].strip()
    key_author = re.sub(r"[^A-Za-z0-9]+", "", first_author) or "paper"
    key_title = re.sub(r"[^A-Za-z0-9]+", "", paper.title or "")[:24] or "untitled"
    base = f"{key_author}{paper.year or ''}{key_title}"
    key = base
    serial = 2
    while key.casefold() in used:
        key = f"{base}-{serial}"
        serial += 1
    used.add(key.casefold())
    return key


def paper_to_bibtex(paper: Paper, used_keys: set[str]) -> str:
    fields: list[tuple[str, str]] = [("title", paper.title or "Untitled")]
    if paper.authors:
        bibtex_authors = " and ".join(
            author.strip() for author in paper.authors.split(";") if author.strip()
        )
        fields.append(("author", bibtex_authors))
    if paper.year:
        fields.append(("year", str(paper.year)))
    if paper.venue:
        fields.append((_booktitle_or_journal(paper), paper.venue))
    if paper.doi:
        fields.append(("doi", paper.doi))
    if paper.url:
        fields.append(("url", paper.url))
    body = ",\n".join(
        f"  {name} = {{{_escape_bibtex(value)}}}" for name, value in fields
    )
    entry_type = _bibtex_entry_type(paper)
    cite_key = _unique_cite_key(paper, used_keys)
    return f"@{entry_type}{{{cite_key},\n{body}\n}}"


def papers_to_bibtex(papers: list[Paper]) -> str:
    used: set[str] = set()
    return "\n\n".join(paper_to_bibtex(paper, used) for paper in papers)


_RIS_TYPES_ARTICLE = "JOUR"


def _ris_type(paper: Paper) -> str:
    return "CONF" if _bibtex_entry_type(paper) == "inproceedings" else _RIS_TYPES_ARTICLE


def paper_to_ris(paper: Paper) -> str:
    lines = [f"TY  - {_ris_type(paper)}"]
    if paper.authors:
        for author in paper.authors.split(";"):
            if author.strip():
                lines.append(f"AU  - {author.strip()}")
    lines.append(f"TI  - {paper.title or 'Untitled'}")
    if paper.year:
        lines.append(f"PY  - {paper.year}")
    if paper.venue:
        tag = "T2" if _ris_type(paper) == "CONF" else "JO"
        lines.append(f"{tag}  - {paper.venue}")
    if paper.doi:
        lines.append(f"DO  - {paper.doi}")
    if paper.url:
        lines.append(f"UR  - {paper.url}")
    if paper.abstract:
        lines.append(f"AB  - {paper.abstract}")
    lines.append("ER  - ")
    return "\n".join(lines)


def papers_to_ris(papers: list[Paper]) -> str:
    return "\n\n".join(paper_to_ris(paper) for paper in papers)


def write_export(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
