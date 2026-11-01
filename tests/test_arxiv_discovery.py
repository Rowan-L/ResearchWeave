from typing import ClassVar

import pytest

from synthrecall.arxiv_discovery import (
    ArxivClient,
    ArxivDownloadCancelledError,
    ArxivRateLimitError,
    DiscoveryCache,
    DiscoveryRecommender,
    normalize_arxiv_query,
    parse_arxiv_feed,
    unique_pdf_path,
)

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2405.12701v1</id>
    <updated>2024-05-19T12:00:00Z</updated>
    <published>2024-05-19T12:00:00Z</published>
    <title>DyConv: Dynamic Convolution for Efficient Visual Recognition</title>
    <summary>
      We propose a dynamic convolution method for efficient visual recognition.
      Extensive experiments on ImageNet and COCO show strong accuracy-efficiency trade-offs.
    </summary>
    <author><name>Chenxi Liu</name></author>
    <author><name>Zhenhang Huang</name></author>
    <arxiv:primary_category term="cs.CV"/>
    <category term="cs.CV"/>
    <category term="cs.LG"/>
    <link href="http://arxiv.org/abs/2405.12701v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2405.12701v1" rel="related" type="application/pdf"/>
  </entry>
</feed>
"""


def test_parse_arxiv_feed_extracts_core_metadata():
    papers = parse_arxiv_feed(SAMPLE_FEED)

    assert len(papers) == 1
    paper = papers[0]
    assert paper.arxiv_id == "2405.12701v1"
    assert paper.title.startswith("DyConv")
    assert paper.authors_text == "Chenxi Liu, Zhenhang Huang"
    assert paper.primary_category == "cs.CV"
    assert paper.pdf_url == "http://arxiv.org/pdf/2405.12701v1"


def test_normalize_arxiv_query_keeps_advanced_queries():
    assert normalize_arxiv_query("cat:cs.CV AND all:transformer") == "cat:cs.CV AND all:transformer"
    assert normalize_arxiv_query("dynamic convolution") == "all:dynamic AND all:convolution"
    assert (
        normalize_arxiv_query("vision transformer OR dynamic convolution")
        == "(all:vision AND all:transformer) OR (all:dynamic AND all:convolution)"
    )


def test_discovery_recommender_scores_relevant_papers():
    paper = parse_arxiv_feed(SAMPLE_FEED)[0]
    score = DiscoveryRecommender(["dynamic convolution", "vision transformer"]).score(paper)

    assert score.overall >= 75
    assert score.relevance >= 6.0
    assert "dynamic convolution" in score.matched_terms
    assert score.reason


def test_unique_pdf_path_prefers_paper_title(tmp_path):
    path = unique_pdf_path(tmp_path, "DyConv: Dynamic Convolution?", "2405.12701v1")

    assert path.name == "DyConv_ Dynamic Convolution.pdf"

    path.write_bytes(b"pdf")
    next_path = unique_pdf_path(tmp_path, "DyConv: Dynamic Convolution?", "2405.12701v1")

    assert next_path.name == "DyConv_ Dynamic Convolution-2405.12701v1.pdf"


def test_discovery_cache_restores_recent_papers(tmp_path):
    cache = DiscoveryCache(tmp_path / "arxiv_discovery_cache.json")
    paper = parse_arxiv_feed(SAMPLE_FEED)[0]
    cache.toggle_bookmark(paper)
    cache.remember_papers([paper])

    restored = cache.recent_paper_list()

    assert len(restored) == 1
    assert restored[0].arxiv_id == paper.arxiv_id
    assert restored[0].title == paper.title
    assert restored[0].is_bookmarked
    assert cache.last_successful_refresh


def test_arxiv_client_tries_direct_connection_before_proxy(monkeypatch):
    attempts: list[bool] = []

    class FakeResponse:
        status_code = 200
        text = SAMPLE_FEED

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *, trust_env: bool, **_kwargs):
            attempts.append(trust_env)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("synthrecall.arxiv_discovery.getproxies", lambda: {"https": "proxy"})
    monkeypatch.setattr("synthrecall.arxiv_discovery.httpx.Client", FakeClient)

    papers = ArxivClient(api_urls=["https://example.test/api"]).search("dynamic convolution")

    assert len(papers) == 1
    assert attempts == [False]


def test_arxiv_client_stops_after_rate_limit(monkeypatch):
    attempts: list[bool] = []

    class FakeResponse:
        status_code = 429
        headers: ClassVar[dict[str, str]] = {}

        def raise_for_status(self):
            raise AssertionError("429 should be handled before raise_for_status")

    class FakeClient:
        def __init__(self, *, trust_env: bool, **_kwargs):
            attempts.append(trust_env)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("synthrecall.arxiv_discovery._wait_for_arxiv_request_slot", lambda: None)
    monkeypatch.setattr("synthrecall.arxiv_discovery.getproxies", lambda: {"https": "proxy"})
    monkeypatch.setattr("synthrecall.arxiv_discovery.httpx.Client", FakeClient)

    with pytest.raises(ArxivRateLimitError, match="HTTP 429"):
        ArxivClient(api_urls=["https://example.test/api"]).search("dynamic convolution")

    assert attempts == [False]


def test_arxiv_rate_limit_reports_retry_after(monkeypatch):
    class FakeResponse:
        status_code = 429
        headers: ClassVar[dict[str, str]] = {"Retry-After": "120"}

        def raise_for_status(self):
            raise AssertionError("429 should be handled before raise_for_status")

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("synthrecall.arxiv_discovery._wait_for_arxiv_request_slot", lambda: None)
    monkeypatch.setattr("synthrecall.arxiv_discovery.getproxies", dict)
    monkeypatch.setattr("synthrecall.arxiv_discovery.httpx.Client", FakeClient)

    with pytest.raises(ArxivRateLimitError, match="2 分钟.*缓存"):
        ArxivClient(api_urls=["https://example.test/api"]).search("dynamic convolution")


def test_arxiv_pdf_download_streams_to_atomic_target(tmp_path, monkeypatch):
    chunks = [b"%PDF-1.7\n", b"large-pdf-body"]

    class FakeResponse:
        headers: ClassVar[dict[str, str]] = {"Content-Length": str(sum(map(len, chunks)))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            assert chunk_size == 256 * 1024
            yield from chunks

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url, **_kwargs):
            assert method == "GET"
            assert url == "https://example.test/paper.pdf"
            return FakeResponse()

    monkeypatch.setattr("synthrecall.arxiv_discovery.httpx.Client", FakeClient)

    target = ArxivClient().download_pdf_url(
        "https://example.test/paper.pdf",
        "Streaming Paper",
        tmp_path,
        "2608.00001",
    )

    assert target.read_bytes() == b"".join(chunks)
    assert not list(tmp_path.glob("*.part"))


def test_arxiv_pdf_download_cancellation_removes_partial_file(tmp_path, monkeypatch):
    cancelled = False

    class FakeResponse:
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            nonlocal cancelled
            yield b"%PDF-1.7\n"
            cancelled = True
            yield b"remaining"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("synthrecall.arxiv_discovery.httpx.Client", FakeClient)

    with pytest.raises(ArxivDownloadCancelledError):
        ArxivClient().download_pdf_url(
            "https://example.test/paper.pdf",
            "Cancelled Paper",
            tmp_path,
            cancel_requested=lambda: cancelled,
        )

    assert list(tmp_path.iterdir()) == []
