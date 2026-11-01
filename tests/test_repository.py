from datetime import UTC
from pathlib import Path

from sqlalchemy import inspect

from synthrecall.database import create_session_factory, init_database
from synthrecall.repositories import (
    DatasetMetricUpdate,
    LibraryRepository,
    PaperUpdate,
    PdfImportMetadata,
    parse_dataset_metrics_text,
)
from synthrecall.seed import seed_demo_data


def write_pdf(path, *, text="SynthRecall import test content", title="", authors=""):
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    if title or authors:
        document.set_metadata({"title": title, "author": authors})
    document.save(path)
    document.close()


def make_repository(tmp_path):
    session_factory = create_session_factory(tmp_path / "library.db")
    init_database(session_factory)
    seed_demo_data(session_factory)
    return LibraryRepository(session_factory)


def test_seed_creates_demo_library(tmp_path):
    repository = make_repository(tmp_path)

    assert len(repository.list_categories()) == 11
    assert len(repository.list_papers()) == 2
    assert repository.list_papers(search="ImageNet")


def test_update_paper_detail_is_searchable(tmp_path):
    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()

    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="Custom Retrieval Paper",
            authors="Demo",
            year=2026,
            venue="Local Test",
            doi="",
            arxiv_id="",
            url="",
            abstract="abstract marker",
            pdf_path="",
            reading_status="已读",
            importance=5,
            summary="summary marker",
            datasets="dataset marker",
            dataset_metrics="dataset: ImageNet; metric: Top-1 Acc; value: 84.5%; model: DemoNet",
            methods="method marker",
            contributions="contribution marker",
            limitations="limitation marker",
            experiment_notes="experiment marker",
            reproducibility_notes="repro marker",
            memory_anchor="anchor marker",
            personal_notes="personal note marker",
        ),
        category_id=None,
    )

    results = repository.list_papers(search="personal note marker")

    assert len(results) == 1
    assert results[0].title == "Custom Retrieval Paper"

    title_only = repository.list_papers(search="anchor marker", search_fields={"title"})
    anchor_only = repository.list_papers(search="anchor marker", search_fields={"memory_anchor"})

    assert title_only == []
    assert len(anchor_only) == 1

    assert repository.list_dataset_metrics() == []


def test_update_paper_can_assign_multiple_categories(tmp_path):
    repository = make_repository(tmp_path)
    first_category = repository.add_category("视觉 Transformer")
    second_category = repository.add_category("大卷积核")
    paper = repository.create_blank_paper()

    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="Multi Tag Paper",
            authors="Demo",
            year=2026,
            venue="Local Test",
            doi="",
            arxiv_id="",
            url="",
            abstract="",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
        category_ids=[first_category.id, second_category.id],
    )

    updated = repository.get_paper(paper.id)

    assert {category.name for category in updated.categories} == {"视觉 Transformer", "大卷积核"}


def test_abstract_translation_cache_does_not_replace_source_abstract(tmp_path):
    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()

    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="Abstract Toggle Paper",
            authors="Demo",
            year=2026,
            venue="Local Test",
            doi="",
            arxiv_id="",
            url="",
            abstract="Original English abstract.",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
        category_id=None,
    )

    repository.set_abstract_translation(paper.id, "Cached Chinese abstract.")

    updated = repository.get_paper(paper.id)
    assert repository.get_abstract_translation(paper.id) == "Cached Chinese abstract."
    assert updated.abstract == "Original English abstract."


def test_reading_status_is_searchable(tmp_path):
    repository = make_repository(tmp_path)

    results = repository.list_papers(search="精读")

    assert results
    assert all("精读" in paper.reading_status for paper in results)


def test_category_context_actions_and_delete_paper(tmp_path):
    repository = make_repository(tmp_path)
    parent = repository.add_category("Context Parent")
    first = repository.add_category("First", parent_id=parent.id)
    second = repository.add_category("Second", parent_id=parent.id)
    paper = repository.create_blank_paper(category_id=first.id)

    repository.rename_category(first.id, "Renamed")
    repository.move_category(second.id, -1)
    moved = [
        category.name
        for category in repository.list_categories()
        if category.parent_id == parent.id
    ]

    assert "Renamed" in moved
    assert moved[0] == "Second"

    repository.delete_paper(paper.id)
    assert all(item.id != paper.id for item in repository.list_papers())

    repository.delete_category(parent.id)
    names = {category.name for category in repository.list_categories()}
    assert "Context Parent" not in names
    assert "Renamed" not in names


def test_import_pdf_folder_skips_duplicates_and_indexes_text(tmp_path):
    repository = make_repository(tmp_path)
    paper_dir = tmp_path / "papers"
    paper_dir.mkdir()
    write_pdf(paper_dir / "import-test.pdf")

    first = repository.import_pdf_folder(paper_dir)
    second = repository.import_pdf_folder(paper_dir)

    assert first.added == 1
    assert second.skipped == 1
    assert repository.list_papers(search="import test content")


def test_paper_list_keeps_full_text_deferred_and_reports_exact_match(tmp_path):
    repository = make_repository(tmp_path)
    pdf_path = tmp_path / "deferred-text.pdf"
    write_pdf(pdf_path, text="unique deferred full text marker")
    imported = repository.import_pdf(pdf_path).paper

    listed = next(paper for paper in repository.list_papers() if paper.id == imported.id)
    assert "text" in inspect(listed).unloaded

    matches = repository.list_papers(
        search="unique deferred full text marker",
        search_fields={"full_text"},
    )
    assert [paper.id for paper in matches] == [imported.id]
    assert matches[0].full_text_match is True
    assert "text" in inspect(matches[0]).unloaded


def test_create_from_pdf_copies_into_managed_category_folder(tmp_path):
    repository = make_repository(tmp_path)
    category = repository.add_category("Managed PDFs")
    source_dir = tmp_path / "external"
    source_dir.mkdir()
    source_pdf = source_dir / "paper.pdf"
    write_pdf(source_pdf)

    paper = repository.create_from_pdf(
        source_pdf,
        category_id=category.id,
        library_paper_dir=tmp_path / "library" / "papers",
    )

    managed_path = tmp_path / "library" / "papers" / category.name / "paper.pdf"
    assert managed_path.exists()
    assert paper.pdf_path == f"library/papers/{category.name}/paper.pdf"
    assert repository.resolve_pdf_path(paper.pdf_path) == managed_path
    assert [item.name for item in paper.categories] == [category.name]
    assert paper.pdf_path != str(source_pdf.resolve())
    assert repository.list_papers(search="import test content")


def test_managed_pdf_path_is_portable_and_legacy_path_is_migrated(tmp_path):
    repository = make_repository(tmp_path)
    managed = tmp_path / "papers" / "Portable" / "paper.pdf"
    managed.parent.mkdir(parents=True)
    write_pdf(managed)
    paper = repository.create_blank_paper()
    old_path = Path("D:/OldInstall/ResearchWeave/data/papers/Portable/paper.pdf")
    with repository._session_factory() as session:
        stored = session.get(type(paper), paper.id)
        stored.pdf_path = str(old_path)
        session.commit()

    migrated = LibraryRepository(repository._session_factory)

    assert migrated.get_paper(paper.id).pdf_path == "papers/Portable/paper.pdf"
    assert migrated.resolve_pdf_path("papers/Portable/paper.pdf") == managed


def test_import_pdf_detects_same_paper_from_normalized_title_and_authors(tmp_path):
    repository = make_repository(tmp_path)
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "revised.pdf"
    managed_dir = tmp_path / "library" / "papers"
    write_pdf(
        first_pdf,
        text="First PDF build",
        title="Attention Is All You Need",
        authors="Ashish Vaswani; Noam Shazeer",
    )
    write_pdf(
        second_pdf,
        text="A different PDF build",
        title="ATTENTION IS ALL YOU NEED!",
        authors="Noam Shazeer, Ashish Vaswani",
    )

    first = repository.import_pdf(first_pdf, library_paper_dir=managed_dir)
    second = repository.import_pdf(second_pdf, library_paper_dir=managed_dir)

    assert first.created is True
    assert second.created is False
    assert second.paper.id == first.paper.id
    assert second.duplicate_reason == "论文标题和作者相同"
    assert len(list(managed_dir.rglob("*.pdf"))) == 1


def test_import_pdf_does_not_merge_same_title_with_different_authors(tmp_path):
    repository = make_repository(tmp_path)
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    write_pdf(
        first_pdf,
        text="First research paper",
        title="A Shared but Distinctive Research Paper Title",
        authors="Alice Example",
    )
    write_pdf(
        second_pdf,
        text="Second research paper",
        title="A Shared but Distinctive Research Paper Title",
        authors="Bob Different",
    )

    first = repository.import_pdf(first_pdf)
    second = repository.import_pdf(second_pdf)

    assert first.created is True
    assert second.created is True
    assert second.paper.id != first.paper.id


def test_import_pdf_detects_doi_even_when_pdf_metadata_differs(tmp_path):
    repository = make_repository(tmp_path)
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    write_pdf(
        first_pdf,
        text="Paper preprint DOI: 10.1234/synthrecall.2026.7",
        title="Preprint Title",
        authors="Alice Example",
    )
    write_pdf(
        second_pdf,
        text="Published version https://doi.org/10.1234/synthrecall.2026.7",
        title="Final Published Title",
        authors="Alice Example; Bob Example",
    )

    first = repository.import_pdf(first_pdf)
    second = repository.import_pdf(second_pdf)

    assert first.created is True
    assert second.created is False
    assert second.paper.id == first.paper.id
    assert second.duplicate_reason.startswith("DOI 相同")


def test_preview_pdf_import_does_not_copy_and_exposes_duplicate(tmp_path):
    repository = make_repository(tmp_path)
    managed_dir = tmp_path / "managed" / "papers"
    first_pdf = tmp_path / "first.pdf"
    duplicate_pdf = tmp_path / "duplicate.pdf"
    write_pdf(
        first_pdf,
        text="First document DOI: 10.1234/preview.1",
        title="A Distinctive Preview Import Paper",
        authors="Alice Example",
    )
    write_pdf(
        duplicate_pdf,
        text="Second document DOI: 10.1234/preview.1",
        title="A Different Display Title",
        authors="Bob Example",
    )
    imported = repository.import_pdf(first_pdf, library_paper_dir=managed_dir)

    preview = repository.preview_pdf_import(
        duplicate_pdf,
        library_paper_dir=managed_dir,
    )

    assert preview.metadata.title == "A Different Display Title"
    assert preview.metadata.doi == "10.1234/preview.1"
    assert preview.duplicate_paper_id == imported.paper.id
    assert preview.duplicate_reason.startswith("DOI 相同")
    assert len(list(managed_dir.rglob("*.pdf"))) == 1


def test_import_pdf_accepts_confirmed_metadata_and_merge_fills_only_missing_fields(tmp_path):
    repository = make_repository(tmp_path)
    category = repository.add_category("Preview Category")
    first_pdf = tmp_path / "first.pdf"
    revised_pdf = tmp_path / "revised.pdf"
    write_pdf(
        first_pdf,
        text="First representation",
        title="A Distinctive Merge Preview Paper",
        authors="",
    )
    write_pdf(
        revised_pdf,
        text="Second representation",
        title="A DISTINCTIVE MERGE PREVIEW PAPER!",
        authors="Alice Example",
    )
    first = repository.import_pdf(first_pdf)
    preview = repository.preview_pdf_import(revised_pdf)
    assert preview.duplicate_paper_id == first.paper.id

    confirmed = PdfImportMetadata(
        title=preview.metadata.title,
        authors="Alice Example",
        year=2026,
        doi="10.1234/confirmed",
        arxiv_id="2607.12345",
        abstract="Confirmed abstract",
    )
    merged = repository.merge_pdf_import(
        revised_pdf,
        first.paper.id,
        confirmed,
        category_id=category.id,
    )

    assert merged.title == "A Distinctive Merge Preview Paper"
    assert merged.authors == "Alice Example"
    assert merged.year == 2026
    assert merged.doi == "10.1234/confirmed"
    assert merged.arxiv_id == "2607.12345"
    assert merged.abstract == "First representation"
    assert merged.pdf_path == "first.pdf"
    assert repository.resolve_pdf_path(merged.pdf_path) == first_pdf.resolve()
    assert [item.id for item in merged.categories] == [category.id]


def test_batch_status_category_and_delete_operations(tmp_path):
    repository = make_repository(tmp_path)
    first = repository.create_blank_paper()
    second = repository.create_blank_paper()
    category = repository.add_category("Batch Category")

    assert repository.set_papers_reading_status([first.id, second.id], "精读") == 2
    assert repository.add_category_to_papers([first.id, second.id], category.id) == 2
    assert repository.add_category_to_papers([first.id, second.id], category.id) == 0

    updated_first = repository.get_paper(first.id)
    updated_second = repository.get_paper(second.id)
    assert updated_first.reading_status == "精读"
    assert updated_second.reading_status == "精读"
    assert [item.id for item in updated_first.categories] == [category.id]

    assert repository.delete_papers([first.id, second.id]) == 2
    remaining_ids = {paper.id for paper in repository.list_papers()}
    assert first.id not in remaining_ids
    assert second.id not in remaining_ids


def _add_keyword_rich_paper(repository):
    """构造一篇命中 CV 关键词规则的文献(旧演示树会把它分到 视觉 Transformer)。"""
    paper = repository.create_blank_paper()
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="Vision Transformer with Dynamic Convolution and Low-rank Filters",
            authors="Demo",
            year=2026,
            venue="Local Test",
            doi="",
            arxiv_id="",
            url="",
            abstract="This paper studies attention, dynamic convolution, ImageNet, and low-rank decomposition.",
            pdf_path="",
            reading_status="待读",
            importance=4,
            summary="",
            datasets="ImageNet",
            dataset_metrics="",
            methods="Dynamic convolution; low-rank; Transformer",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="attention generated kernel",
            personal_notes="",
        ),
        category_id=None,
    )
    return paper


def test_fresh_library_seeds_only_uncategorized(tmp_path):
    """B34:全新库不再种入演示分类树(研究主题/阅读状态),自动分类也不会
    把它们灌回来;命中关键词但库里没有同名分类时按未命中处理。"""
    session_factory = create_session_factory(tmp_path / "auto.db")
    init_database(session_factory)
    repository = LibraryRepository(session_factory)
    repository.ensure_default_categories()
    paper = _add_keyword_rich_paper(repository)

    result = repository.auto_classify_all()
    top_names = {
        category.name
        for category in repository.list_categories()
        if category.parent_id is None
    }

    assert top_names == {"未分类"}, top_names
    assert result.created_categories == 0
    classified = repository.get_paper(paper.id)
    assert {category.name for category in classified.categories} == {"未分类"}


def test_auto_classify_assigns_existing_category_match(tmp_path):
    """F19 关键词匹配仍有效:库里已有同名分类时,命中即挂为唯一主分类。"""
    session_factory = create_session_factory(tmp_path / "auto2.db")
    init_database(session_factory)
    repository = LibraryRepository(session_factory)
    repository.ensure_default_categories()
    repository.add_category("视觉 Transformer")
    paper = _add_keyword_rich_paper(repository)

    repository.auto_classify_all()
    classified = repository.get_paper(paper.id)

    # F19 单主分类:多规则命中时只挂得分最高的一个(细粒度特征交给标签)
    assert {category.name for category in classified.categories} == {
        "视觉 Transformer"
    }


def test_parse_dataset_metrics_text_accepts_json_and_lines():
    json_rows = parse_dataset_metrics_text(
        '[{"dataset": "COCO", "metric": "mAP", "value": "46.2", "model": "DemoNet"}]'
    )
    line_rows = parse_dataset_metrics_text(
        "数据集: ImageNet; 指标: Top-1 Acc; 结果: 84.5%; 模型: DemoNet; 任务: classification"
    )

    assert json_rows[0]["dataset"] == "COCO"
    assert json_rows[0]["metric"] == "mAP"
    assert line_rows[0]["dataset"] == "ImageNet"
    assert line_rows[0]["value"] == "84.5%"


def test_dataset_ranking_uses_manual_entries(tmp_path):
    repository = make_repository(tmp_path)
    entry = repository.add_dataset_metric_entry(
        DatasetMetricUpdate(
            dataset="DeepGlobe Roads dataset",
            metric="IoU",
            value="71.66%",
            model="UMiT-Net",
            task="Road Segmentation",
            notes="manual leaderboard",
            unit="%",
            direction="higher",
        )
    )

    rows = repository.list_dataset_metrics()

    assert len(rows) == 1
    assert rows[0].entry_id == entry.id
    assert rows[0].model == "UMiT-Net"
    assert rows[0].metric == "IoU"
    assert rows[0].unit == "%"
    assert rows[0].direction == "higher"

    repository.update_dataset_metric_entry(
        entry.id,
        DatasetMetricUpdate(
            dataset="DeepGlobe Roads dataset",
            metric="F1",
            value="82.61%",
            model="UMiT-Net",
            unit="%",
            direction="lower",
        ),
    )
    updated = repository.list_dataset_metrics()
    assert updated[0].metric == "F1"
    assert updated[0].numeric_value == 82.61
    assert updated[0].direction == "lower"

    repository.delete_dataset_metric_entry(entry.id)
    assert repository.list_dataset_metrics() == []


def test_dataset_names_are_canonicalized_for_ranking_and_ai_prompt(tmp_path):
    repository = make_repository(tmp_path)
    first = repository.create_blank_paper()
    second = repository.create_blank_paper()

    base_update = {
        "authors": "Demo",
        "year": 2026,
        "venue": "Local Test",
        "doi": "",
        "arxiv_id": "",
        "url": "",
        "abstract": "",
        "pdf_path": "",
        "reading_status": "待读",
        "importance": 3,
        "summary": "",
        "datasets": "",
        "methods": "",
        "contributions": "",
        "limitations": "",
        "experiment_notes": "",
        "reproducibility_notes": "",
        "memory_anchor": "",
        "personal_notes": "",
    }
    repository.update_paper(first.id, PaperUpdate(title="ImageNet Full Name", dataset_metrics="", **base_update), category_id=None)
    repository.update_paper(second.id, PaperUpdate(title="ImageNet Short Name", dataset_metrics="", **base_update), category_id=None)
    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(dataset="ImageNet-1K", metric="Top-1 Acc", value="84.5%", model="ModelA")
    )
    repository.add_dataset_metric_entry(
        DatasetMetricUpdate(dataset="ImageNet", metric="Top-1 Acc", value="83.1%", model="ModelB")
    )

    rows = repository.list_dataset_metrics()
    imagenet_rows = [row for row in rows if row.model in {"ModelA", "ModelB"}]
    source = repository.get_ai_source(second.id, max_chars=1000)

    assert {row.dataset for row in imagenet_rows} == {"ImageNet-1K"}
    assert "ImageNet-1K" in source["known_datasets"]


def test_new_import_honors_user_selected_category(tmp_path):
    repository = make_repository(tmp_path)
    requested_category = repository.add_category("Requested During Import")
    source_pdf = tmp_path / "new-import.pdf"
    write_pdf(
        source_pdf,
        title="Always Initially Uncategorized",
        authors="Manual Classifier",
    )

    imported = repository.import_pdf(
        source_pdf,
        category_id=requested_category.id,
        library_paper_dir=tmp_path / "papers",
    ).paper
    assert [category.name for category in imported.categories] == [
        "Requested During Import"
    ]
    assert "Requested During Import" in Path(imported.pdf_path).parts

    current = repository.get_paper(imported.id)
    repository.update_paper(
        imported.id,
        PaperUpdate(
            title=current.title,
            authors=current.authors,
            year=current.year,
            venue=current.venue,
            doi=current.doi,
            arxiv_id=current.arxiv_id,
            url=current.url,
            abstract=current.abstract,
            pdf_path=current.pdf_path,
            reading_status=current.reading_status,
            importance=current.importance,
            summary=current.detail.summary,
            datasets=current.detail.datasets,
            dataset_metrics=current.detail.dataset_metrics,
            methods=current.detail.methods,
            contributions=current.detail.contributions,
            limitations=current.detail.limitations,
            experiment_notes=current.detail.experiment_notes,
            reproducibility_notes=current.detail.reproducibility_notes,
            memory_anchor=current.detail.memory_anchor,
            personal_notes=current.detail.personal_notes,
        ),
        category_ids=[requested_category.id],
    )
    manually_classified = repository.get_paper(imported.id)
    assert [category.name for category in manually_classified.categories] == [
        "Requested During Import"
    ]


def test_clearing_manual_categories_restores_uncategorized(tmp_path):
    repository = make_repository(tmp_path)
    category = repository.add_category("Manual Category")
    paper = repository.create_blank_paper(category_id=category.id)
    current = repository.get_paper(paper.id)
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title=current.title,
            authors=current.authors,
            year=current.year,
            venue=current.venue,
            doi=current.doi,
            arxiv_id=current.arxiv_id,
            url=current.url,
            abstract=current.abstract,
            pdf_path=current.pdf_path,
            reading_status=current.reading_status,
            importance=current.importance,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
        category_ids=[],
    )
    assert [item.name for item in repository.get_paper(paper.id).categories] == ["未分类"]


def test_full_text_search_uses_fts_for_chinese_and_english(tmp_path):
    repository = make_repository(tmp_path)

    paper = repository.create_blank_paper()
    # 直接写入正文,验证事件同步的 FTS 索引
    with repository._session_factory() as session:
        from synthrecall.models import PaperText

        session.merge(PaperText(paper_id=paper.id, content="曼哈顿距离衰减机制 Retentive Networks", page_count=1))
        session.commit()

    fields = {"full_text"}
    hits_cjk = repository.list_papers(search="曼哈顿", search_fields=fields)
    hits_en = repository.list_papers(search="retent", search_fields=fields)
    hits_short = repository.list_papers(search="卷积", search_fields=fields)  # 2 字回退 ILIKE(种子里无此文,应不命中)

    assert any(p.id == paper.id for p in hits_cjk), "中文 3 字子串应命中 FTS"
    assert any(p.id == paper.id for p in hits_en), "英文子串(忽略大小写)应命中 FTS"
    assert not hits_short

    badge = next(p for p in hits_cjk if p.id == paper.id)
    assert badge.full_text_match is True, "全文命中的记录应带 full_text_match 标记"


def test_full_text_index_follows_text_delete_and_replace(tmp_path):
    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()
    with repository._session_factory() as session:
        from synthrecall.models import PaperText

        session.merge(PaperText(paper_id=paper.id, content="unique_marker_old", page_count=1))
        session.commit()

    fields = {"full_text"}
    assert repository.list_papers(search="unique_marker_old", search_fields=fields)

    with repository._session_factory() as session:
        from synthrecall.models import PaperText

        text_row = session.get(PaperText, paper.id)
        text_row.content = "unique_marker_new"
        session.commit()
    assert not repository.list_papers(search="unique_marker_old", search_fields=fields)
    assert repository.list_papers(search="unique_marker_new", search_fields=fields)

    repository.delete_paper(paper.id)
    assert not repository.list_papers(search="unique_marker_new", search_fields=fields)


def test_trash_restore_and_purge_lifecycle(tmp_path):
    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()
    with repository._session_factory() as session:
        from synthrecall.models import PaperText

        session.merge(PaperText(paper_id=paper.id, content="trash lifecycle marker text", page_count=1))
        session.commit()
    fields = {"full_text"}

    # 软删除:列表/全文搜索隐藏,但 FTS 行保留(记录仍在,恢复后可搜)
    assert repository.list_papers(search="lifecycle marker", search_fields=fields)
    repository.delete_paper(paper.id)
    assert not repository.list_papers(search="lifecycle marker", search_fields=fields)
    assert all(p.id != paper.id for p in repository.list_papers())
    assert [p.id for p in repository.list_trashed_papers()] == [paper.id]

    # 恢复:搜索与列表恢复可见
    assert repository.restore_papers([paper.id]) == 1
    assert repository.list_papers(search="lifecycle marker", search_fields=fields)

    # 彻底删除:记录消失且 FTS 同步清理
    repository.delete_paper(paper.id)
    repository.purge_papers([paper.id])
    assert not repository.list_trashed_papers()
    with repository._session_factory() as session:
        from sqlalchemy import text

        fts_left = session.execute(text("SELECT COUNT(*) FROM paper_texts_fts WHERE rowid = :pid"), {"pid": paper.id}).scalar_one()
        texts_left = session.execute(text("SELECT COUNT(*) FROM paper_texts WHERE paper_id = :pid"), {"pid": paper.id}).scalar_one()
    assert fts_left == 0 and texts_left == 0, "purge 后 FTS 与正文同步清理"


def test_purged_paper_keeps_dataset_metric_entries(tmp_path):
    # B4 回归:彻底删除文献后,排行榜条目应保留(paper_id 置空),历史不丢
    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()
    update = DatasetMetricUpdate(
        dataset="ImageNet",
        metric="Top-1",
        value="84.1",
        unit="%",
        direction="higher",
        model="RMT-L",
        task="分类",
        notes="",
        paper_id=paper.id,
    )
    entry = repository.add_dataset_metric_entry(update)

    repository.delete_papers([paper.id])
    repository.purge_papers([paper.id])

    with repository._session_factory() as session:
        from synthrecall.models import DatasetMetricEntry

        kept = session.get(DatasetMetricEntry, entry.id)
    assert kept is not None, "指标条目不应随文献删除"
    assert kept.paper_id is None, "paper_id 应置空"
    assert kept.model == "RMT-L"


def test_tag_crud_filter_and_paper_assignment(tmp_path):
    repository = make_repository(tmp_path)
    paper_a = repository.create_blank_paper()
    paper_b = repository.create_blank_paper()

    tag = repository.create_tag("动态卷积", color="#ef4444")
    duplicate = repository.create_tag("动态卷积")
    assert duplicate.id == tag.id, "同名标签应复用"

    repository.update_paper(
        paper_a.id,
        PaperUpdate(
            title="Tagged A",
            authors="",
            year=None,
            venue="",
            doi="",
            arxiv_id="",
            url="",
            abstract="",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
        tag_ids=[tag.id],
    )
    repository.update_paper(
        paper_b.id,
        PaperUpdate(
            title="Tagged B",
            authors="",
            year=None,
            venue="",
            doi="",
            arxiv_id="",
            url="",
            abstract="",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
    )

    hits = repository.list_papers(tag_id=tag.id)
    assert [p.id for p in hits] == [paper_a.id]
    assert repository.tag_paper_counts().get(tag.id) == 1

    repository.set_tag_color(tag.id, "#22c55e")
    repository.rename_tag(tag.id, "重命名标签")
    refreshed = next(t for t in repository.list_tags() if t.id == tag.id)
    assert refreshed.name == "重命名标签"
    assert refreshed.color == "#22c55e"

    repository.delete_tag(tag.id)
    assert all(t.id != tag.id for t in repository.list_tags()), "标签应被删除"
    assert repository.list_papers()  # 文献不受影响


def test_citation_export_bibtex_and_ris(tmp_path):
    from synthrecall.citation_export import papers_to_bibtex, papers_to_ris

    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()
    repository.update_paper(
        paper.id,
        PaperUpdate(
            title="Attention & Memory: 100% verified_study",
            authors="Alice A; Bob B",
            year=2024,
            venue="CVPR 2024",
            doi="10.1000/example",
            arxiv_id="",
            url="https://example.org",
            abstract="An abstract with & special chars.",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
    )
    paper = repository.get_paper(paper.id, include_text_content=False)

    bib = papers_to_bibtex([paper])
    assert bib.startswith("@inproceedings{"), "CVPR 应识别为会议论文"
    assert r"Attention \& Memory" in bib, "& 应转义"
    assert r"100\% verified\_study" in bib, "% 与 _ 应转义"
    assert "author = {Alice A and Bob B}" in bib, "作者分号应转 and"
    assert "booktitle = {CVPR 2024}" in bib

    # 同批导出两篇 cite key 不冲突
    paper2 = repository.create_blank_paper()
    repository.update_paper(
        paper2.id,
        PaperUpdate(
            title=paper.title,
            authors=paper.authors,
            year=paper.year,
            venue="",
            doi="",
            arxiv_id="",
            url="",
            abstract="",
            pdf_path="",
            reading_status="待读",
            importance=3,
            summary="",
            datasets="",
            dataset_metrics="",
            methods="",
            contributions="",
            limitations="",
            experiment_notes="",
            reproducibility_notes="",
            memory_anchor="",
            personal_notes="",
        ),
    )
    papers = [
        repository.get_paper(paper.id, include_text_content=False),
        repository.get_paper(paper2.id, include_text_content=False),
    ]
    bib2 = papers_to_bibtex(papers)
    keys = [
        line.split("{", 1)[1].split(",", 1)[0]
        for line in bib2.splitlines()
        if line.startswith("@")
    ]
    assert len(keys) == len(set(keys)), "同批 cite key 必须唯一"

    ris = papers_to_ris(papers)
    assert ris.count("TY  - ") == 2
    assert "AU  - Alice A" in ris
    assert ris.count("ER  - ") == 2


def test_list_papers_smart_group_filters(tmp_path):
    repository = make_repository(tmp_path)
    specs = [
        ("S1", "精读", 2024, 5),
        ("S2", "待读", 2024, 3),
        ("S3", "已读", 2023, 4),
    ]
    from synthrecall.repositories import PaperUpdate

    for title, status, year, importance in specs:
        paper = repository.create_blank_paper()
        repository.update_paper(
            paper.id,
            PaperUpdate(
                title=title, authors="", year=year, venue="", doi="", arxiv_id="",
                url="", abstract="", pdf_path="", reading_status=status,
                importance=importance, summary="", datasets="", dataset_metrics="",
                methods="", contributions="", limitations="", experiment_notes="",
                reproducibility_notes="", memory_anchor="", personal_notes="",
            ),
        )

    titles = lambda **kw: {p.title for p in repository.list_papers(**kw)}
    assert "S1" in titles(reading_status="精读")
    assert "S2" in titles(reading_status="待读")
    assert titles(year=2024) >= {"S1", "S2"}
    assert {"S1", "S3"} <= titles(min_importance=4)


def test_metadata_enrichment_parsing_and_fill_only_missing(tmp_path, qtbot, monkeypatch):
    import pytest

    import synthrecall.metadata_enrichment as me
    from synthrecall.metadata_enrichment import fetch_metadata

    # 无标识符直接报错
    with pytest.raises(ValueError):
        fetch_metadata()

    # CrossRef 解析
    def fake_crossref(self, url, **kwargs):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"message": {
                    "title": ["Deep <b>Learning</b> Survey"],
                    "author": [{"given": "Alice", "family": "A"}, {"given": "Bob", "family": "B"}],
                    "published-online": {"date-parts": [[2024, 6, 1]]},
                    "container-title": ["CVPR 2024"],
                    "DOI": "10.1000/x",
                    "URL": "https://doi.org/10.1000/x",
                }}
        return R()

    monkeypatch.setattr(me.httpx.Client, "get", fake_crossref)
    meta = fetch_metadata(doi="10.1000/x")
    assert meta.title == "Deep <b>Learning</b> Survey"
    assert meta.authors == "Alice A; Bob B"
    assert meta.year == 2024
    assert meta.venue == "CVPR 2024"

    # arXiv 解析
    def fake_arxiv(self, url, **kwargs):
        class R:
            def raise_for_status(self): pass
            @property
            def text(self):
                return ('<entry><title>Retentive Networks</title><summary>An abstract</summary>'
                        '<published>2024-06-01T00:00:00Z</published>'
                        '<author><name>Qihang Fan</name></author>'
                        '<arxiv:doi>10.48550/arXiv.2307</arxiv:doi></entry>')
        return R()

    monkeypatch.setattr(me.httpx.Client, "get", fake_arxiv)
    meta = fetch_metadata(arxiv_id="2307.00001")
    assert meta.title == "Retentive Networks"
    assert meta.authors == "Qihang Fan"
    assert meta.year == 2024
    assert meta.venue == "arXiv 2307.00001"



def test_legacy_local_timestamps_migrated_to_utc(tmp_path):
    """B6 回归:user_version=0 的旧库,本地时间列应整体平移为 UTC 且幂等。"""
    from datetime import datetime

    from sqlalchemy import create_engine, text

    from synthrecall.database import create_session_factory, init_database

    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE papers ("
            " id INTEGER PRIMARY KEY, title VARCHAR(500) DEFAULT '', authors VARCHAR(500) DEFAULT '',"
            " year INTEGER, venue VARCHAR(300) DEFAULT '', doi VARCHAR(200) DEFAULT '',"
            " arxiv_id VARCHAR(100) DEFAULT '', url VARCHAR(500) DEFAULT '', abstract TEXT DEFAULT '',"
            " pdf_path VARCHAR(1000) DEFAULT '', file_hash VARCHAR(128) DEFAULT '',"
            " reading_status VARCHAR(50) DEFAULT '待读', importance INTEGER DEFAULT 3,"
            " created_at DATETIME, updated_at DATETIME, last_opened_at DATETIME, deleted_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO papers (created_at, updated_at)"
            " VALUES ('2026-01-01 08:00:00', '2026-01-01 08:00:00')"
        ))

    factory = create_session_factory(db)
    init_database(factory)
    offset = datetime.now().astimezone().utcoffset()
    expected = (datetime(2026, 1, 1, 8, 0) - offset).strftime("%Y-%m-%d %H:%M:%S")
    with factory() as session:
        row = session.execute(text("SELECT created_at, updated_at FROM papers")).first()
        assert row[0].startswith(expected), f"存量应平移为 UTC: {row[0]} != {expected}"
        applied = {
            int(row[0])
            for row in session.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
        assert 3 in applied, "UTC 迁移应登记在 schema_migrations"
        assert session.execute(text("PRAGMA user_version")).scalar_one() == max(applied), (
            "user_version 应同步为最大已应用迁移号"
        )

    init_database(factory)  # 幂等:二次初始化不得再次平移
    with factory() as session:
        row_again = session.execute(text("SELECT created_at FROM papers")).first()
        assert row_again[0] == row[0], "迁移必须幂等"


def test_new_rows_written_in_utc(tmp_path):
    from datetime import datetime

    repository = make_repository(tmp_path)
    paper = repository.create_blank_paper()
    got = repository.get_paper(paper.id, include_text_content=False).updated_at
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    assert abs((got - now_utc).total_seconds()) < 5, "新写入应为 UTC(naive)"


def test_backup_and_restore_roundtrip_with_wal(tmp_path):
    """F5 回归:写入不 checkpoint 也能完整备份(WAL 合并),恢复可回滚。"""
    import zipfile

    from synthrecall.backup import create_backup, read_manifest, restore_backup

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    repository = LibraryRepository(factory)

    marker = repository.create_blank_paper()
    repository.update_paper(
        marker.id,
        PaperUpdate(
            title="备份点标记文献", authors="", year=None, venue="", doi="",
            arxiv_id="", url="", abstract="", pdf_path="", reading_status="待读",
            importance=3, summary="", datasets="", dataset_metrics="", methods="",
            contributions="", limitations="", experiment_notes="",
            reproducibility_notes="", memory_anchor="", personal_notes="",
        ),
    )
    # 不做 checkpoint(默认 WAL 模式),直接备份——必须包含最新写入
    backup_path = create_backup(data_dir, tmp_path / "backup.zip")
    assert backup_path.exists()
    manifest = read_manifest(backup_path)
    assert manifest["file_count"] >= 1
    with zipfile.ZipFile(backup_path) as archive:
        db_names = [n for n in archive.namelist() if n.endswith(".db")]
        assert db_names, "备份应包含数据库文件"
        assert any(n == "manifest.json" for n in archive.namelist())

    # 备份点之后继续改动:新建第二篇 + 删除第一篇
    repository.create_blank_paper()
    repository.delete_papers([marker.id])
    assert all(p.id != marker.id for p in repository.list_papers())

    # 恢复 → 回到备份点:标记文献回来了,第二篇消失
    report = restore_backup(backup_path, data_dir)
    assert report.restored_files >= 1
    assert report.safety_backup is not None and report.safety_backup.exists()
    factory2 = create_session_factory(data_dir / "library.db")
    init_database(factory2)
    repository2 = LibraryRepository(factory2)
    titles = {p.title for p in repository2.list_papers()}
    assert "备份点标记文献" in titles
    assert repository2.get_paper(marker.id) is not None


def test_backup_covers_all_user_data_dirs(tmp_path):
    """F5 完整性(用户报告恢复后数据不全):译文/原件副本/导出目录必须进备份,
    babeldoc_home(缓存)与 backups(备份自身)排除。"""
    import zipfile

    from synthrecall.backup import create_backup, restore_backup

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    LibraryRepository(factory)

    translations = data_dir / "translations" / "p1"
    translations.mkdir(parents=True)
    (translations / "paper.zh.mono.pdf").write_bytes(b"%PDF-1.4 translated")
    originals = data_dir / "papers"
    originals.mkdir()
    (originals / "p1.pdf").write_bytes(b"%PDF-1.4 original")
    (data_dir / "exports").mkdir()
    (data_dir / "exports" / "notes.md").write_text("# 导出", encoding="utf-8")
    (data_dir / "pdf_backups").mkdir()
    (data_dir / "pdf_backups" / "keep.pdf").write_bytes(b"%PDF-1.4 backup")
    cache = data_dir / "babeldoc_home" / ".cache"
    cache.mkdir(parents=True)
    (cache / "model.onnx").write_bytes(b"cache")

    backup_path = create_backup(data_dir, tmp_path / "backup.zip")
    with zipfile.ZipFile(backup_path) as archive:
        names = archive.namelist()
    assert "translations/p1/paper.zh.mono.pdf" in names
    assert "papers/p1.pdf" in names
    assert "exports/notes.md" in names
    assert "pdf_backups/keep.pdf" in names
    assert not any(n.startswith("babeldoc_home/") for n in names), "缓存不应进备份"

    # 破坏现场后恢复:译文与导出都回来了
    import shutil

    shutil.rmtree(data_dir / "translations")
    shutil.rmtree(data_dir / "exports")
    report = restore_backup(backup_path, data_dir)
    assert report.restored_files >= 4
    assert (data_dir / "translations" / "p1" / "paper.zh.mono.pdf").read_bytes() == (
        b"%PDF-1.4 translated"
    )
    assert (data_dir / "exports" / "notes.md").read_text(encoding="utf-8") == "# 导出"
    assert (data_dir / "pdf_backups" / "keep.pdf").exists()


def test_restore_rejects_invalid_and_malicious_backups(tmp_path):
    from synthrecall.backup import read_manifest, restore_backup

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)

    # 非 ZIP / 缺 manifest
    bad = tmp_path / "bad.zip"
    import zipfile as zf

    with zf.ZipFile(bad, "w") as archive:
        archive.writestr("random.txt", "x")
    import pytest

    with pytest.raises(ValueError, match="manifest"):
        read_manifest(bad)
    with pytest.raises(ValueError, match="manifest"):
        restore_backup(bad, data_dir)

    # 路径穿越条目必须被拒
    evil = tmp_path / "evil.zip"
    with zf.ZipFile(evil, "w") as archive:
        archive.writestr("manifest.json", '{"file_count": 1, "files": []}')
        archive.writestr("../../evil.txt", "boom")
    with pytest.raises(ValueError, match="非法路径"):
        restore_backup(evil, data_dir)
    assert not (tmp_path / "evil.txt").exists()


def test_migration_framework_registry_and_idempotence(tmp_path):
    """B10 回归:迁移按序登记、幂等,legacy 探测区分新旧库。"""
    from sqlalchemy import create_engine, text

    from synthrecall.database import _is_legacy_database, create_session_factory, init_database

    # 新库:legacy=False,迁移 3 不应平移任何数据
    factory = create_session_factory(tmp_path / "new.db")
    init_database(factory)
    with factory() as session:
        applied = [
            (int(v), name)
            for v, name in session.execute(
                text("SELECT version, name FROM schema_migrations ORDER BY version")
            ).fetchall()
        ]
        assert [v for v, _ in applied] == [1, 2, 3, 4, 5, 6], applied
        assert applied[0][1] == "add-missing-columns"
        assert applied[2][1] == "timestamps-to-utc"
        # 再次 init:登记不变(幂等)
        init_database(factory)
        again = session.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
        assert again == 6

    with factory() as session:
        assert not _is_legacy_database(session.connection())

    # 旧库:user_version 兼容路径——手工把登记清空后 user_version 仍>=1,
    # 框架不得重复执行 UTC 平移(迁移 3 视为已应用)
    db = tmp_path / "compat.db"
    legacy_engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    with legacy_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE papers ("
            " id INTEGER PRIMARY KEY, title VARCHAR(500) DEFAULT '', authors VARCHAR(500) DEFAULT '',"
            " year INTEGER, venue VARCHAR(300) DEFAULT '', doi VARCHAR(200) DEFAULT '',"
            " arxiv_id VARCHAR(100) DEFAULT '', url VARCHAR(500) DEFAULT '', abstract TEXT DEFAULT '',"
            " pdf_path VARCHAR(1000) DEFAULT '', file_hash VARCHAR(128) DEFAULT '',"
            " reading_status VARCHAR(50) DEFAULT '待读', importance INTEGER DEFAULT 3,"
            " created_at DATETIME, updated_at DATETIME, last_opened_at DATETIME, deleted_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO papers (created_at, updated_at) VALUES ('2026-01-01 08:00:00', '2026-01-01 08:00:00')"
        ))
    factory2 = create_session_factory(db)
    init_database(factory2)  # legacy → 平移 -8h
    init_database(factory2)  # 幂等
    with factory2() as session:
        row = session.execute(text("SELECT created_at FROM papers")).first()
        assert row[0].startswith("2026-01-01 00:00:00"), f"平移且仅平移一次: {row[0]}"
        applied2 = {
            int(row[0])
            for row in session.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
        assert applied2 == {1, 2, 3, 4, 5, 6}


def _add_raw_paper(session_factory, **fields):
    from synthrecall.models import Paper

    with session_factory() as session:
        paper = Paper(**fields)
        session.add(paper)
        session.commit()
        return paper.id


def test_find_duplicate_groups_detection(tmp_path):
    """F12:库内重复扫描——文件哈希/DOI/标题作者判定成组,唯一与回收站文献不入组。"""
    from synthrecall.database import create_session_factory, init_database

    factory = create_session_factory(tmp_path / "dup.db")
    init_database(factory)
    repository = LibraryRepository(factory)
    repository.ensure_default_categories()

    p1 = _add_raw_paper(
        factory,
        title="Contrastive Graph Pretraining via Subspace Alignment",
        authors="Alice Chen; David Wu",
        doi="10.1000/xyz",
    )
    p2 = _add_raw_paper(
        factory,
        title="Contrastive  Graph Pretraining via Subspace  Alignment!!",
        authors="David Wu; Alice Chen",
    )
    p3 = _add_raw_paper(
        factory,
        title="An Entirely Different Paper About Topic Models",
        doi="10.1000/XYZ",  # 大小写不同仍判 DOI 相同
    )
    p4 = _add_raw_paper(factory, title="A Unique Solo Contribution Here")
    p5 = _add_raw_paper(factory, title="Hash Twin First Record", file_hash="deadbeef")
    p6 = _add_raw_paper(factory, title="Hash Twin Second Record", file_hash="deadbeef")

    groups = repository.find_duplicate_groups()
    by_members = {frozenset(group.paper_ids): group for group in groups}
    assert frozenset({p1, p2, p3}) in by_members, groups
    group = by_members[frozenset({p1, p2, p3})]
    assert "标题和作者相同" in group.reason and "DOI 相同" in group.reason
    assert frozenset({p5, p6}) in by_members
    assert by_members[frozenset({p5, p6})].reason == "PDF 文件内容相同"
    assert len(groups) == 2 and p4 not in {pid for g in groups for pid in g.paper_ids}

    # 回收站文献不参与扫描:移入回收站后 p1/p2 仍成组,p3 退出
    repository.delete_papers([p3])
    groups = repository.find_duplicate_groups()
    by_members = {frozenset(group.paper_ids): group for group in groups}
    assert frozenset({p1, p2}) in by_members
    assert all(p3 not in g.paper_ids for g in groups)


def test_merge_duplicate_papers_fills_missing_and_unions(tmp_path):
    """F12:合并只补保留记录缺失的字段,已填字段不覆盖;分类/标签取并集,PDF 随迁。"""
    from synthrecall.database import create_session_factory, init_database
    from synthrecall.models import Paper

    factory = create_session_factory(tmp_path / "merge.db")
    init_database(factory)
    repository = LibraryRepository(factory)
    repository.ensure_default_categories()

    keeper = _add_raw_paper(
        factory,
        title="Keeper Paper Title Here",
        authors="Alice Chen",
        doi="10.1000/keep",
    )
    duplicate = _add_raw_paper(
        factory,
        title="Keeper Paper Title Here",
        authors="Alice Chen",
        year=2024,
        venue="NeurIPS 2024",
        abstract="Rich abstract from duplicate",
    )
    duplicate_pdf = tmp_path / "duplicate.pdf"
    write_pdf(duplicate_pdf, text="merged pdf body", title="Keeper Paper Title Here", authors="Alice Chen")
    with factory() as session:
        session.get(Paper, duplicate).pdf_path = str(duplicate_pdf)
        session.commit()

    tag = repository.create_tag("复现必看")
    from synthrecall.models import Tag

    with factory() as session:
        duplicate_row = session.get(Paper, duplicate)
        duplicate_row.tags.append(session.get(Tag, tag.id))
        session.commit()

    counts = repository.merge_duplicate_papers(keeper, [duplicate])
    assert counts["metadata"] >= 3 and counts["tags"] == 1 and counts["pdf"] == 1

    with factory() as session:
        merged = session.get(Paper, keeper)
        assert merged.year == 2024
        assert merged.venue == "NeurIPS 2024"
        assert merged.abstract == "Rich abstract from duplicate"
        assert merged.doi == "10.1000/keep"  # 已填字段不被覆盖
        assert merged.pdf_path  # 收编了重复记录的 PDF
        assert [t.name for t in merged.tags] == ["复现必看"]

    repo_after = LibraryRepository(factory)
    repo_after.delete_papers([duplicate])
    assert len(repo_after.list_trashed_papers()) == 1
    groups = repo_after.find_duplicate_groups()
    assert all(duplicate not in group.paper_ids for group in groups)


def test_library_statistics_aggregates_for_panel(tmp_path):
    """F8:统计聚合——状态/分类/年份计数、深度阅读、回收站排除、UTC→本地月桶补零。"""
    from datetime import datetime

    from synthrecall.database import create_session_factory, init_database
    from synthrecall.models import Category, Paper

    factory = create_session_factory(tmp_path / "stats.db")
    init_database(factory)
    repository = LibraryRepository(factory)
    repository.ensure_default_categories()
    cat = repository.add_category("统计分类")

    def add(title, *, status="待读", year=None, pdf="", created=None, categorized=False):
        paper_id = _add_raw_paper(
            factory,
            title=title,
            reading_status=status,
            year=year,
            pdf_path=pdf,
            created_at=created,
        )
        if categorized:
            with factory() as session:
                paper = session.get(Paper, paper_id)
                paper.categories.append(session.get(Category, cat.id))
                session.commit()
        return paper_id

    # 月桶断言全部相对"当前月"构造,避免测试跨月滚动后失效:
    # - 文A:上月末 20:00 UTC → 本地(UTC+8)本月 1 日 04:00,跨月边界落本月桶(B6 展示语义)
    # - 文B:本月 1 日往前 15 天 → 上月中旬,落上月桶
    # - 回收站文:同落上月桶,验证回收站不计入
    from datetime import timedelta

    local_first = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = local_first.strftime("%Y-%m")
    last_month_first = (local_first - timedelta(days=1)).replace(day=1)
    last_month = last_month_first.strftime("%Y-%m")
    prev_month = (last_month_first - timedelta(days=1)).replace(day=1).strftime("%Y-%m")

    add("月桶九月", status="精读", year=2023, pdf="a.pdf", created=local_first - timedelta(days=15), categorized=True)
    # UTC 上月末 20:00 → 本地本月 1 日 04:00,跨月边界落本月桶(与原 9-30 20:00 UTC 同构)
    add("月桶十月", status="待读", year=2023, created=local_first - timedelta(hours=4), categorized=True)
    add("今年新文", status="已复现", year=2024, pdf="c.pdf", created=local_first + timedelta(hours=20))
    trashed_id = add("回收站文", status="浏览", created=local_first - timedelta(days=10))
    repository.delete_papers([trashed_id])

    stats = repository.library_statistics(trend_months=3)
    assert stats.total_papers == 3 and stats.trashed_count == 1
    assert stats.pdf_count == 2
    assert stats.deep_reading_count == 2  # 精读 + 已复现
    assert stats.reading_status_counts == (("待读", 1), ("精读", 1), ("已复现", 1))
    assert stats.category_counts == (("统计分类", 2),)
    assert stats.year_counts == ((2023, 2), (2024, 1))

    months = dict(stats.import_month_counts)
    assert set(months) == {prev_month, last_month, this_month}
    assert months[prev_month] == 0  # 窗口补零
    assert months[last_month] == 1  # 仅文B;回收站文不计
    assert months[this_month] == 2  # 跨月边界文A + 新文
    # 回收站文献不进任何分布
    assert "浏览" not in dict(stats.reading_status_counts)
