"""F20 智能文件夹(保存的搜索)测试:迁移/CRUD/规则回环/GUI 树集成。"""

from synthrecall.database import create_session_factory, init_database
from synthrecall.models import Paper, PaperDetail
from synthrecall.repositories import (
    LibraryRepository,
    SavedSearchRule,
)
from synthrecall.ui.main_window import SAVED_SEARCH_NONE


def make_repo(tmp_path):
    factory = create_session_factory(tmp_path / "lib.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    return repo


def add_paper(repo, title, *, status="待读", year=None, tag=None, datasets=""):
    with repo._session_factory() as session:
        paper = Paper(title=title, reading_status=status, year=year)
        paper.detail = PaperDetail(datasets=datasets)
        session.add(paper)
        session.commit()
        paper_id = paper.id
    if tag:
        repo.add_tag_to_paper(paper_id, tag)
    return paper_id


def test_migration_creates_table_and_is_idempotent(tmp_path):
    from sqlalchemy import inspect

    factory = create_session_factory(tmp_path / "mig.db")
    init_database(factory)
    assert inspect(factory.kw["bind"]).has_table("saved_searches")
    init_database(factory)  # 幂等,不抛错


def test_saved_search_crud_and_rule_roundtrip(tmp_path):
    repo = make_repo(tmp_path)
    tag = repo.create_tag("复现必看")
    rule = SavedSearchRule(
        search="光流",
        tag_id=tag.id,
        reading_status="待读",
        year=2026,
        min_importance=3,
    )
    entry = repo.create_saved_search("2026 待读含光流", rule)
    assert entry.name == "2026 待读含光流"

    entries = repo.list_saved_searches()
    assert len(entries) == 1
    restored = entries[0].rule
    assert restored == rule, "JSON 回环后规则逐字段一致"

    repo.update_saved_search(entry.saved_search_id, "改名", SavedSearchRule(year=2025))
    updated = repo.list_saved_searches()[0]
    assert updated.name == "改名" and updated.rule.year == 2025 and updated.rule.search == ""

    repo.delete_saved_search(entry.saved_search_id)
    assert repo.list_saved_searches() == []


def test_rule_kwargs_actually_filter(tmp_path):
    repo = make_repo(tmp_path)
    tag = repo.create_tag("复现必看")
    hit = add_paper(repo, "命中文献", status="待读", year=2026, tag="复现必看", datasets="Sintel")
    miss_status = add_paper(repo, "状态不符", status="已读", year=2026)
    miss_year = add_paper(repo, "年份不符", status="待读", year=2024)

    rule = SavedSearchRule(reading_status="待读", year=2026, tag_id=tag.id)
    ids = [p.id for p in repo.list_papers(**rule.to_kwargs())]
    assert hit in ids and miss_status not in ids and miss_year not in ids

    keyword_rule = SavedSearchRule(search="Sintel")
    ids2 = [p.id for p in repo.list_papers(**keyword_rule.to_kwargs())]
    assert hit in ids2 and miss_status not in ids2


def test_gui_saved_folder_tree_and_filter(tmp_path, qtbot, monkeypatch):
    from PySide6.QtCore import Qt

    from synthrecall.ui.main_window import MainWindow

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    hit = add_paper(repo, "待读 2026 命中", status="待读", year=2026)
    add_paper(repo, "已读 2026", status="已读", year=2026)
    add_paper(repo, "待读 2024", status="待读", year=2024)

    window = MainWindow(repository=repo, data_dir=data_dir)
    window.discovery_page.initial_feed_requested = True
    window._prompt_window_close_action = lambda: "exit"
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    rule = SavedSearchRule(reading_status="待读", year=2026)
    saved_search_id = repo.create_saved_search("待读 2026", rule).saved_search_id
    window.refresh_categories()
    qtbot.wait(20)

    # 树中出现「智能文件夹」父节点与子节点(带计数 1)
    parent_item = None
    for index in range(window.category_tree.topLevelItemCount()):
        item = window.category_tree.topLevelItem(index)
        if item.data(0, Qt.ItemDataRole.UserRole) == SAVED_SEARCH_NONE:
            parent_item = item
            break
    assert parent_item is not None and parent_item.childCount() == 1
    child = parent_item.child(0)
    assert "待读 2026" in child.text(0) and "1" in child.text(0)

    # 点击子节点 → 列表只剩命中文献
    window.category_tree.setCurrentItem(child)
    qtbot.wait(20)
    assert window.current_saved_search_id == saved_search_id
    titles = [
        window.paper_list.item(row).data(Qt.ItemDataRole.UserRole)
        for row in range(window.paper_list.count())
    ]
    assert titles == [hit]

    # 点父节点 = 取消筛选
    window.category_tree.setCurrentItem(parent_item)
    qtbot.wait(20)
    assert window.current_saved_search_id is None
    assert window.paper_list.count() == 3


def test_gui_create_prefills_current_filters(tmp_path, qtbot, monkeypatch):
    from synthrecall.ui.main_window import MainWindow
    from synthrecall.ui.saved_search_dialog import SavedSearchDialog

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    add_paper(repo, "待读 2026 目标", status="待读", year=2026)
    add_paper(repo, "已读干扰", status="已读", year=2026)

    window = MainWindow(repository=repo, data_dir=data_dir)
    window.discovery_page.initial_feed_requested = True
    window._prompt_window_close_action = lambda: "exit"
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    # 用户先在界面上筛选出「待读 2026」
    window.smart_filter = ("smart", "待读")
    window.refresh_papers()
    assert window.paper_list.count() == 1

    # 「把当前筛选存为智能文件夹」→ 对话框预填当前状态 → 保存
    created = {}

    class FakeDialog(SavedSearchDialog):
        def __init__(self, repository, *, name="", rule=None, parent=None):
            super().__init__(repository, name=name, rule=rule, parent=parent)
            created["prefilled_rule"] = self.build_rule()
            self.name_edit.setText("我的待读 2026")
            # 现有智能分组是单选,组合维度(再加年份)正是在对话框里补的
            self.year_spin.setValue(2026)

        def exec(self):
            self.accept()
            return 1

    monkeypatch.setattr(
        __import__("synthrecall.ui.main_window", fromlist=["SavedSearchDialog"]),
        "SavedSearchDialog",
        FakeDialog,
    )
    window.create_saved_search_from_current()

    entries = repo.list_saved_searches()
    assert len(entries) == 1 and entries[0].name == "我的待读 2026"
    assert entries[0].rule.reading_status == "待读", "预填携带当前智能分组(待读)"
    assert created["prefilled_rule"].year is None, "预填=当前筛选状态(单维度)"
    assert entries[0].rule.year == 2026, "对话框中补的年份进入规则(组合维度)"
    # 建完即应用
    assert window.current_saved_search_id == entries[0].saved_search_id
    assert window.paper_list.count() == 1
