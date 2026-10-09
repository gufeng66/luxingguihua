"""
智能体看到的「虚拟硬盘」

【小白怎么理解？】
    模型读写的是 /workspace/results/… 这种 Unix 风格路径，不是 Windows 盘符。
    CompositeBackend 把前缀映射到真实文件夹；高德 Skill 再包一层 ReadOnlyBackend，
    防止模型改写 skill 脚本。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import (
    PERMISSION_DENIED,
    DeleteResult,
    EditResult,
    FileUploadResponse,
    WriteResult,
)

from planner.paths import CONFIG_DIR, CONSOLE_ENCODING, RESULTS_DIR, SKILL_DIR

# ponytail: deepagents 用 text=True 读 shell。paths 把 locale 改成 utf-8 后，中文 Windows 上 cmd/node 的 GBK 会让读线程崩掉，模型看不到命令输出。
if sys.platform == "win32" and not getattr(subprocess.run, "_console_text", False):
    _subprocess_run = subprocess.run

    def _run_console_text(*args: Any, **kwargs: Any):
        if kwargs.get("text") or kwargs.get("universal_newlines"):
            kwargs.setdefault("encoding", CONSOLE_ENCODING or "utf-8")
            kwargs.setdefault("errors", "replace")
        return _subprocess_run(*args, **kwargs)

    _run_console_text._console_text = True  # type: ignore[attr-defined]
    subprocess.run = _run_console_text  # type: ignore[assignment]


class ReadOnlyBackend:
    """只读包装：禁止写入 skill 目录。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error="skill 目录只读，禁止写入")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error="skill 目录只读，禁止修改")

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error="skill 目录只读，禁止删除")

    async def adelete(self, file_path: str) -> DeleteResult:
        return self.delete(file_path)

    def upload_files(self, files: Any, *args: Any, **kwargs: Any) -> list[FileUploadResponse]:
        paths = files if isinstance(files, list) else []
        return [
            FileUploadResponse(path=str(item.get("path") if isinstance(item, dict) else item), error=PERMISSION_DENIED)
            for item in paths
        ]

    async def aupload_files(self, files: Any, *args: Any, **kwargs: Any) -> list[FileUploadResponse]:
        return self.upload_files(files, *args, **kwargs)


# ponytail: host shell, no sandbox. StateBackend has no execute, so map_agent never ran node and always hit the dispatch timeout. cwd is the skill dir so `node scripts/...` matches SKILL.md.
# PATH 等只为让 node 能启动；密钥不继承。容器沙箱再隔离文件系统。
_SHELL_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP")


def shell_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _SHELL_KEEP if os.environ.get(key)}
    amap = (os.environ.get("AMAP_KEY") or os.environ.get("AMAP_WEBSERVICE_KEY") or "").strip()
    if amap:
        env["AMAP_KEY"] = amap
    return env


backend = CompositeBackend(
    default=LocalShellBackend(root_dir=str(SKILL_DIR), inherit_env=False, env=shell_env()),
    routes={
        "/workspace/results/": FilesystemBackend(root_dir=str(RESULTS_DIR), virtual_mode=True),
        "/workspace/config/": FilesystemBackend(root_dir=str(CONFIG_DIR), virtual_mode=True),
        "/workspace/skills/amap-lbs-skill/": ReadOnlyBackend(
            FilesystemBackend(root_dir=str(SKILL_DIR), virtual_mode=True)
        ),
    },
)
