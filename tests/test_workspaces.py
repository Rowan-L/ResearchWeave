"""F11 工作区注册表测试:增删/当前标记/启动回退。"""

from synthrecall.workspaces import (
    add_workspace,
    current_workspace,
    list_workspaces,
    remove_workspace,
    resolve_startup_root,
    set_current,
)


def test_workspace_registry_crud_and_current(tmp_path):
    registry = tmp_path / "workspaces.json"

    main = add_workspace("主库", tmp_path / "lib-main", path=registry)
    assert (main.root / "data").is_dir(), "注册时应自动创建 data 子目录"

    add_workspace("论文实验库", tmp_path / "lib-exp", path=registry)
    names = [entry.name for entry in list_workspaces(registry)]
    assert names == ["主库", "论文实验库"]

    # 首个注册的成为 current
    assert current_workspace(registry).name == "主库"

    set_current("论文实验库", path=registry)
    assert current_workspace(registry).root == (tmp_path / "lib-exp").resolve()

    # 同名更新:改路径不产生重复
    add_workspace("论文实验库", tmp_path / "lib-exp2", path=registry)
    entries = {entry.name: entry for entry in list_workspaces(registry)}
    assert set(entries) == {"主库", "论文实验库"}
    assert entries["论文实验库"].root == (tmp_path / "lib-exp2").resolve()

    # 移除非当前
    remove_workspace("主库", path=registry)
    assert [entry.name for entry in list_workspaces(registry)] == ["论文实验库"]
    # 移除当前 → current 清空
    remove_workspace("论文实验库", path=registry)
    assert current_workspace(registry) is None


def test_resolve_startup_root_falls_back(tmp_path, monkeypatch):
    from pathlib import Path

    import synthrecall.workspaces as ws

    registry = tmp_path / "workspaces.json"
    monkeypatch.setattr(ws, "project_root", lambda: tmp_path)

    fallback = Path("C:/fallback-install")
    # 无注册表 → 回退
    assert resolve_startup_root(fallback) == fallback

    # 有当前工作区且 data 存在 → 用它
    add_workspace("科研主库", tmp_path / "ws1", make_current=True, path=registry)
    assert resolve_startup_root(fallback) == (tmp_path / "ws1").resolve()

    # current 指向的目录被删 → 回退(启动不崩)
    import shutil

    shutil.rmtree(tmp_path / "ws1" / "data")
    assert resolve_startup_root(fallback) == fallback
