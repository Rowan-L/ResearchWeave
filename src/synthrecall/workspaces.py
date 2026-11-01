"""F11 多工作区:工作区(库目录)注册表。

注册表是项目根下的 workspaces.json(与 data/ 平级,不属于任何单个工作区),
记录多个工作区目录与最近使用的一个;GUI 启动时据此选择库目录,
切换工作区 = 更新注册表 + 重启进程(数据库会话/全文索引随工作区整体重建)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REGISTRY_FILE_NAME = "workspaces.json"


@dataclass(frozen=True)
class WorkspaceEntry:
    name: str
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


def registry_path(project_root: Path) -> Path:
    return project_root / REGISTRY_FILE_NAME


def project_root() -> Path:
    """安装/项目根:打包版取 exe 所在目录,开发态取 CWD(run_demo 已 chdir)。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _load(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"current": "", "items": []}
    if not isinstance(payload, dict):
        return {"current": "", "items": []}
    items = [
        {"name": str(item.get("name", "")).strip(), "root": str(item.get("root", ""))}
        for item in payload.get("items", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
        and str(item.get("root", "")).strip()
    ]
    return {"current": str(payload.get("current", "")), "items": items}


def _save(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_workspaces(path: Path | None = None) -> list[WorkspaceEntry]:
    path = path or registry_path(project_root())
    return [
        WorkspaceEntry(name=item["name"], root=Path(item["root"]))
        for item in _load(path)["items"]
    ]


def current_workspace(path: Path | None = None) -> WorkspaceEntry | None:
    path = path or registry_path(project_root())
    payload = _load(path)
    for item in payload["items"]:
        if item["name"] == payload["current"]:
            return WorkspaceEntry(name=item["name"], root=Path(item["root"]))
    return None


def add_workspace(name: str, root: Path, *, make_current: bool = False, path: Path | None = None) -> WorkspaceEntry:
    """注册(或更新同名)工作区;目录不存在时自动创建 data 子目录。"""
    path = path or registry_path(project_root())
    name = name.strip()
    if not name:
        raise ValueError("工作区名称不能为空。")
    root = root.expanduser().resolve()
    (root / "data").mkdir(parents=True, exist_ok=True)
    payload = _load(path)
    payload["items"] = [
        item for item in payload["items"] if item["name"] != name
    ]
    payload["items"].append({"name": name, "root": str(root)})
    if make_current or not payload["current"]:
        payload["current"] = name
    _save(path, payload)
    return WorkspaceEntry(name=name, root=root)


def remove_workspace(name: str, path: Path | None = None) -> None:
    """仅从注册表移除(不动磁盘数据);若移除的是当前工作区则清空 current。"""
    path = path or registry_path(project_root())
    payload = _load(path)
    payload["items"] = [item for item in payload["items"] if item["name"] != name]
    if payload["current"] == name:
        payload["current"] = ""
    _save(path, payload)


def set_current(name: str, path: Path | None = None) -> None:
    """标记当前工作区;name 不在注册表(如内置默认工作区)时清空 current,
    启动将回退到安装目录。"""
    path = path or registry_path(project_root())
    payload = _load(path)
    payload["current"] = name if any(
        item["name"] == name for item in payload["items"]
    ) else ""
    _save(path, payload)


def resolve_startup_root(fallback: Path) -> Path:
    """GUI 启动用:当前工作区 root;无注册/无 current 时用 fallback(安装目录)。"""
    entry = current_workspace()
    if entry is not None and (entry.root / "data").is_dir():
        return entry.root
    return fallback
