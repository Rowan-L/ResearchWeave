"""F19 主分类+标签二分法测试:数据层收敛语义、AI 建议解析、向导与建议条。"""

from synthrecall.ai import LLMClient, LLMSettings
from synthrecall.database import create_session_factory, init_database
from synthrecall.models import Category, Paper
from synthrecall.repositories import LibraryRepository


def make_repo(tmp_path):
    factory = create_session_factory(tmp_path / "lib.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    return repo


def add_paper(factory, title, **fields):
    with factory() as session:
        paper = Paper(title=title, **fields)
        session.add(paper)
        session.commit()
        return paper.id


def _categories_of(repo, paper_id):
    return [c.name for c in repo.get_paper(paper_id).categories]


def _tags_of(repo, paper_id):
    return [t.name for t in repo.get_paper(paper_id).tags]


def test_set_primary_category_single_semantics(tmp_path):
    repo = make_repo(tmp_path)
    cat_a = repo.add_category("主题A")
    cat_b = repo.add_category("主题B")
    paper_id = add_paper(repo._session_factory, "单主分类语义")

    repo.set_primary_category(paper_id, cat_a.id)
    assert _categories_of(repo, paper_id) == ["主题A"]
    repo.set_primary_category(paper_id, cat_b.id)
    assert _categories_of(repo, paper_id) == ["主题B"], "切换即替换,不叠加"
    repo.set_primary_category(paper_id, None)
    assert _categories_of(repo, paper_id) == ["未分类"]


def test_add_tag_to_paper_idempotent(tmp_path):
    repo = make_repo(tmp_path)
    paper_id = add_paper(repo._session_factory, "标签幂等")
    assert repo.add_tag_to_paper(paper_id, "幂等标签") is True
    assert repo.add_tag_to_paper(paper_id, "幂等标签") is False, "同名复用不重复挂"
    assert _tags_of(repo, paper_id) == ["幂等标签"]


def test_demote_categories_to_tags(tmp_path):
    repo = make_repo(tmp_path)
    theme = repo.add_category("研究主题")
    task_cat = repo.add_category("任务级分类")  # 维度错位分类,待降级
    child = repo.add_category("子分类", parent_id=theme.id)
    paper1 = add_paper(repo._session_factory, "文献一")
    paper2 = add_paper(repo._session_factory, "文献二")
    with repo._session_factory() as session:
        for pid in (paper1, paper2):
            paper = session.get(Paper, pid)
            paper.categories.append(session.get(Category, task_cat.id))
        session.get(Paper, paper1).categories.append(session.get(Category, child.id))
        session.commit()

    applied = repo.demote_categories_to_tags([task_cat.id])

    assert applied == {paper1: ["任务级分类"], paper2: ["任务级分类"]}
    assert _tags_of(repo, paper1) == ["任务级分类"]
    assert "任务级分类" not in [c.name for c in repo.list_categories()], "节点应删除"
    assert _categories_of(repo, paper1) == ["子分类"], "paper1 仍保留其它分类"
    assert _categories_of(repo, paper2) == ["未分类"], "解绑后无分类挂未分类"
    # 有子节点的分类只解绑不删除
    applied2 = repo.demote_categories_to_tags([theme.id])
    assert applied2 == {}
    assert any(c.name == "研究主题" for c in repo.list_categories())


def test_suggest_category_prompt_and_parse(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "视觉 Transformer"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, endpoint, headers=None, json=None):
            captured["payload"] = json
            return FakeResponse()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("synthrecall.ai.httpx.Client", FakeClient)
    client = LLMClient(
        LLMSettings(base_url="https://x.test/v1", model="m", max_chars=100),
        api_key="k",
    )
    source = {"title": "T", "abstract": "A", "summary": "S", "memory_anchor": "MA"}
    answer = client.suggest_category(source, ["视觉 Transformer", "动态卷积"])
    assert answer == "视觉 Transformer"
    user_text = captured["payload"]["messages"][1]["content"]
    assert "视觉 Transformer" in user_text and "动态卷积" in user_text


def test_category_cleanup_dialog_flow(tmp_path, qtbot):
    from synthrecall.ui.category_cleanup import CategoryCleanupDialog

    repo = make_repo(tmp_path)
    cat_a = repo.add_category("主题分类A")
    cat_b = repo.add_category("特征分类B")
    paper_id = add_paper(repo._session_factory, "向导文献")
    with repo._session_factory() as session:
        paper = session.get(Paper, paper_id)
        paper.categories = [session.get(Category, cat_a.id), session.get(Category, cat_b.id)]
        session.commit()

    dialog = CategoryCleanupDialog(repo)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)

    assert dialog.paper_table.rowCount() == 1
    combo = dialog._primary_combos[paper_id]
    assert combo.currentText() == "主题分类A", "默认保留第一个正式分类"
    assert dialog.paper_table.item(0, 3).text() == "特征分类B", "其余分类将转标签"

    dialog.execute_cleanup()

    assert _categories_of(repo, paper_id) == ["主题分类A"]
    assert _tags_of(repo, paper_id) == ["特征分类B"]
    assert "整理完成" in dialog.status_label.text()
    # 再跑一次幂等:没有多余分类可转
    dialog.execute_cleanup()
    assert _categories_of(repo, paper_id) == ["主题分类A"]
    assert _tags_of(repo, paper_id) == ["特征分类B"]


def test_ai_suggestion_bar_apply_and_new(tmp_path, qtbot):
    from synthrecall.models import PaperDetail
    from synthrecall.ui.main_window import MainWindow

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    factory = create_session_factory(data_dir / "library.db")
    init_database(factory)
    repo = LibraryRepository(factory)
    repo.ensure_default_categories()
    paper_id = add_paper(factory, "建议条文献", abstract="关于视觉变换器的研究")
    with factory() as session:
        session.get(Paper, paper_id).detail = PaperDetail(paper_id=paper_id)
        session.commit()
    window = MainWindow(repository=repo, data_dir=data_dir)
    window.discovery_page.initial_feed_requested = True  # 防 arXiv 自动拉取
    window._prompt_window_close_action = lambda: "exit"  # 防 teardown 关窗模态
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)
    window.select_paper(paper_id)

    repo.add_category("视觉变换器")
    window.refresh_categories()

    # 已有分类的建议 → 一键应用
    window._on_category_suggestion(paper_id, "视觉变换器")
    assert window.category_suggestion_bar.isVisible()
    window._apply_suggested_category()
    assert _categories_of(repo, paper_id) == ["视觉变换器"]
    assert not window.category_suggestion_bar.isVisible()

    # NEW:: 建议 → 建新分类并设为主分类
    window._on_category_suggestion(paper_id, "NEW::光流估计")
    window._apply_suggested_category()
    assert _categories_of(repo, paper_id) == ["光流估计"]
    assert any(c.name == "光流估计" for c in repo.list_categories())
