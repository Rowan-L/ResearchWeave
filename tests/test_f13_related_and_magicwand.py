"""F13 相关文献推荐 + 魔棒按标识符添加 测试。"""

from synthrecall.database import create_session_factory, init_database
from synthrecall.models import Paper, PaperDetail
from synthrecall.repositories import LibraryRepository


def make_repo(tmp_path):
    factory = create_session_factory(tmp_path / "lib.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    return repo


def add_paper(repo, title, *, anchor="", tags=(), category=None, datasets="", metrics=""):
    with repo._session_factory() as session:
        paper = Paper(title=title)
        paper.detail = PaperDetail(
            datasets=datasets,
            dataset_metrics=metrics,
            memory_anchor=anchor,
        )
        session.add(paper)
        session.commit()
        paper_id = paper.id
    for tag_name in tags:
        repo.add_tag_to_paper(paper_id, tag_name)
    if category:
        cat = next(c for c in repo.list_categories() if c.name == category)
        repo.set_primary_category(paper_id, cat.id)
    return paper_id


def test_parse_identifier_variants():
    from synthrecall.ui.main_window import MainWindow
    assert MainWindow._parse_identifier("10.1000/abc.def") == ("doi", "10.1000/abc.def")
    assert MainWindow._parse_identifier("https://doi.org/10.1000/xyz") == ("doi", "10.1000/xyz")
    assert MainWindow._parse_identifier("arXiv:2603.25739") == ("arxiv", "2603.25739")
    assert MainWindow._parse_identifier("https://arxiv.org/abs/2603.25739v2") == (
        "arxiv",
        "2603.25739v2",
    )
    assert MainWindow._parse_identifier("2603.25739") == ("arxiv", "2603.25739")
    assert MainWindow._parse_identifier("https://example.com/paper") == (
        "url",
        "https://example.com/paper",
    )
    assert MainWindow._parse_identifier("随便一句话") == ("", "")


def test_create_identifier_paper_and_duplicate(tmp_path):
    from synthrecall.metadata_enrichment import PaperMetadata

    repo = make_repo(tmp_path)
    meta = PaperMetadata(
        title="Magic Wand Paper",
        authors="A; B",
        year=2026,
        venue="CVPR",
        abstract="Abs",
        doi="10.1000/mw",
        arxiv_id="",
        url="",
    )
    paper, duplicate = repo.create_identifier_paper(meta)
    assert paper is not None and duplicate is None
    created = repo.get_paper(paper.id)
    assert created.title == "Magic Wand Paper" and created.doi == "10.1000/mw"
    assert [c.name for c in created.categories] == ["未分类"]

    paper2, duplicate2 = repo.create_identifier_paper(
        PaperMetadata(title="别的标题", doi="10.1000/mw")
    )
    assert paper2 is None and duplicate2 is not None and duplicate2.id == paper.id


def test_related_papers_scoring_and_exclusion(tmp_path):
    repo = make_repo(tmp_path)
    repo.add_category("视觉 Transformer")
    base = add_paper(
        repo,
        "目标文献",
        anchor="零样本光流;全局匹配",
        tags=("语义分割", "低秩与压缩"),
        category="视觉 Transformer",
        datasets="Sintel",
        metrics="KITTI",
    )
    strong = add_paper(
        repo,
        "强相关文献",
        anchor="零样本光流;别的",
        tags=("语义分割",),
        category="视觉 Transformer",
        datasets="Sintel",
        metrics="KITTI",
    )
    weak = add_paper(repo, "弱相关文献", tags=("语义分割",))
    unrelated = add_paper(repo, "无关文献")
    trashed = add_paper(repo, "回收站文献", tags=("语义分割",))

    related = repo.related_papers(base, limit=5)
    ids = [p.id for p, _s, _r in related]
    assert strong in ids and weak in ids
    assert base not in ids and unrelated not in ids
    scores = {p.id: s for p, s, _r in related}
    assert scores[strong] > scores[weak]
    reasons_by_id = {p.id: r for p, _s, r in related}
    top_reasons = reasons_by_id[strong]
    assert any("标签" in reason for reason in top_reasons)
    assert any("同主分类" in reason for reason in top_reasons)
    assert any("数据集" in reason for reason in top_reasons)
    assert any("锚点" in reason for reason in top_reasons)

    repo.delete_papers([trashed])
    assert trashed not in [p.id for p, _s, _r in repo.related_papers(base)]


def test_gui_related_list_renders_and_navigates(tmp_path, qtbot):
    from PySide6.QtCore import Qt

    from synthrecall.ui.main_window import MainWindow

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    repo.add_category("视觉 Transformer")

    with factory() as session:
        for title in ("主文献", "相关文献甲", "相关文献乙"):
            paper = Paper(title=title)
            paper.detail = PaperDetail(datasets="Sintel")
            session.add(paper)
        session.commit()
    for paper in repo.list_papers():
        if paper.title != "主文献":
            repo.add_tag_to_paper(paper.id, "共享标签")
    main_id = next(p.id for p in repo.list_papers() if p.title == "主文献")

    window = MainWindow(repository=repo, data_dir=data_dir)
    window.discovery_page.initial_feed_requested = True
    window._prompt_window_close_action = lambda: "exit"
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)
    window.select_paper(main_id)
    qtbot.wait(20)

    assert window.related_papers_list.isVisible()
    assert window.related_papers_list.count() == 2
    first_item = window.related_papers_list.item(0)
    target_id = first_item.data(Qt.ItemDataRole.UserRole)
    window._open_related_paper(first_item)
    qtbot.wait(20)
    assert window.current_paper_id == target_id


def test_gui_magic_wand_creates_paper(tmp_path, qtbot, monkeypatch):
    from synthrecall.metadata_enrichment import PaperMetadata
    from synthrecall.ui.main_window import MainWindow

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    with factory() as session:
        seed_paper = Paper(title="已有文献")
        seed_paper.detail = PaperDetail()
        session.add(seed_paper)
        session.commit()

    window = MainWindow(repository=repo, data_dir=data_dir)
    window.discovery_page.initial_feed_requested = True
    window._prompt_window_close_action = lambda: "exit"
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    captured = {}

    def fake_fetch(doi="", arxiv_id="", timeout=20.0):
        captured["kind"] = (doi, arxiv_id)
        return PaperMetadata(
            title="Wand Created Paper",
            authors="X; Y",
            year=2026,
            venue="arXiv",
            abstract="abs",
            doi="",
            arxiv_id="2603.25739",
            url="https://arxiv.org/abs/2603.25739",
        )

    import synthrecall.ui.main_window as mw

    monkeypatch.setattr(mw, "fetch_metadata", fake_fetch)
    monkeypatch.setattr(
        mw.QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: ("https://arxiv.org/abs/2603.25739v2", True)),
    )
    monkeypatch.setattr(mw, "arxiv_pdf_url", lambda value: "https://arxiv.org/pdf/2603.25739v2")
    monkeypatch.setattr(window, "_enqueue_task", lambda *a, **k: captured.setdefault("enqueued", True))
    monkeypatch.setattr(mw.QMessageBox, "question", staticmethod(lambda *a, **k: mw.QMessageBox.StandardButton.Yes))

    window.add_by_identifier()
    qtbot.waitUntil(
        lambda: any(p.title == "Wand Created Paper" for p in repo.list_papers()),
        timeout=3000,
    )

    assert captured["kind"] == ("", "2603.25739v2")
    created = next(p for p in repo.list_papers() if p.title == "Wand Created Paper")
    assert window.current_paper_id == created.id
    assert captured.get("enqueued"), "arXiv 文献应自动入队 PDF 下载"
    assert [c.name for c in created.categories] == ["未分类"]
