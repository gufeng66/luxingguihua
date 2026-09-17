"""智能体虚拟文件系统：results/config 可写，skill 只读。"""

from __future__ import annotations

from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import (
    PERMISSION_DENIED,
    DeleteResult,
    EditResult,
    FileUploadResponse,
    WriteResult,
)

from planner.paths import CONFIG_DIR, RESULTS_DIR, SKILL_DIR


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


backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/workspace/results/": FilesystemBackend(root_dir=str(RESULTS_DIR), virtual_mode=True),
        "/workspace/config/": FilesystemBackend(root_dir=str(CONFIG_DIR), virtual_mode=True),
        "/workspace/skills/amap-lbs-skill/": ReadOnlyBackend(
            FilesystemBackend(root_dir=str(SKILL_DIR), virtual_mode=True)
        ),
    },
)
