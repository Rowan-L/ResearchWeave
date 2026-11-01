import pytest

from synthrecall.paper_url import (
    PaperURLImportCancelled,
    PaperURLImporter,
    PaperURLImportError,
    arxiv_pdf_url,
    normalize_paper_url,
)


class FakeResponse:
    def __init__(
        self,
        url: str,
        body: bytes,
        content_type: str,
        *,
        disposition: str = "",
    ) -> None:
        self.url = url
        self.body = body
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        if disposition:
            self.headers["Content-Disposition"] = disposition
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int):
        assert chunk_size == 256 * 1024
        yield self.body


def fake_client(monkeypatch, responses: dict[str, FakeResponse], requested: list[str]) -> None:
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method: str, url: str, **_kwargs):
            assert method == "GET"
            requested.append(url)
            return responses[url]

    monkeypatch.setattr("synthrecall.paper_url.httpx.Client", FakeClient)


def test_normalize_and_arxiv_resolution():
    assert normalize_paper_url(" https://example.test/paper#abstract ") == (
        "https://example.test/paper"
    )
    assert arxiv_pdf_url("https://arxiv.org/abs/2405.12701v2") == (
        "https://arxiv.org/pdf/2405.12701v2"
    )
    assert arxiv_pdf_url("https://example.test/abs/2405.12701v2") is None
    with pytest.raises(PaperURLImportError, match="http"):
        normalize_paper_url("not-a-url")


def test_download_recognizes_extensionless_pdf_by_magic(tmp_path, monkeypatch):
    url = "https://example.test/download?id=42"
    requested: list[str] = []
    fake_client(
        monkeypatch,
        {
            url: FakeResponse(
                url,
                b"%PDF-1.7\nbody",
                "application/octet-stream",
                disposition='attachment; filename="actual-paper.pdf"',
            )
        },
        requested,
    )

    result = PaperURLImporter().download(url, tmp_path)

    assert result.path.name == "actual-paper.pdf"
    assert result.path.read_bytes() == b"%PDF-1.7\nbody"
    assert result.pdf_url == url
    assert requested == [url]


def test_download_finds_citation_pdf_url_on_paper_page(tmp_path, monkeypatch):
    page_url = "https://publisher.test/article/42"
    pdf_url = "https://cdn.publisher.test/files/paper-42"
    page = (
        '<html><head><meta name="citation_pdf_url" '
        f'content="{pdf_url}"></head></html>'
    ).encode()
    requested: list[str] = []
    fake_client(
        monkeypatch,
        {
            page_url: FakeResponse(page_url, page, "text/html; charset=utf-8"),
            pdf_url: FakeResponse(pdf_url, b"%PDF-1.6\npaper", "application/pdf"),
        },
        requested,
    )

    result = PaperURLImporter().download(page_url, tmp_path)

    assert result.path.read_bytes().startswith(b"%PDF-")
    assert result.pdf_url == pdf_url
    assert requested == [page_url, pdf_url]


def test_arxiv_abstract_link_tries_canonical_pdf_first(tmp_path, monkeypatch):
    page_url = "https://arxiv.org/abs/2405.12701v2"
    pdf_url = "https://arxiv.org/pdf/2405.12701v2"
    requested: list[str] = []
    fake_client(
        monkeypatch,
        {pdf_url: FakeResponse(pdf_url, b"%PDF-1.7\narxiv", "application/pdf")},
        requested,
    )

    result = PaperURLImporter().download(page_url, tmp_path)

    assert result.pdf_url == pdf_url
    assert requested == [pdf_url]


def test_non_pdf_candidate_is_rejected_and_partial_file_removed(tmp_path, monkeypatch):
    url = "https://example.test/fake.pdf"
    requested: list[str] = []
    fake_client(
        monkeypatch,
        {url: FakeResponse(url, b"<html><body>not a paper</body></html>", "text/html")},
        requested,
    )

    with pytest.raises(PaperURLImportError, match="有效 PDF"):
        PaperURLImporter().download(url, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_cancelled_download_removes_partial_file(tmp_path, monkeypatch):
    url = "https://example.test/paper.pdf"
    requested: list[str] = []
    fake_client(
        monkeypatch,
        {url: FakeResponse(url, b"%PDF-1.7\nbody", "application/pdf")},
        requested,
    )

    with pytest.raises(PaperURLImportCancelled):
        PaperURLImporter().download(url, tmp_path, cancel_requested=lambda: True)

    assert list(tmp_path.iterdir()) == []
