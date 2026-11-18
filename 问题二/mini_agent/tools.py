"""本地工作区工具及其参数校验。"""

import difflib
import fnmatch
import os
import re
import stat
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .shell import run


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListFiles(Args, title="列出文件"):
    path: str = Field(".", title="目录路径")
    pattern: str | None = Field(None, title="匹配模式", description="相对于 path 的文件匹配模式，例如 **/*.py")


class ReadFile(Args, title="读取文件"):
    path: str = Field(title="文件路径")
    start_line: int = Field(1, ge=1, title="起始行号", description="读取从该行开始，包含该行")
    end_line: int | None = Field(None, ge=1, title="结束行号", description="读取到该行结束，包含该行")


class WriteFile(Args, title="写入文件"):
    path: str = Field(title="文件路径")
    content: str = Field(title="文件内容")


class EditFile(Args, title="编辑文件"):
    path: str = Field(title="文件路径")
    old_string: str = Field(min_length=1, title="待替换文本")
    new_string: str = Field(title="替换后的文本")


class Grep(Args, title="搜索文本"):
    pattern: str = Field(title="正则表达式", description="逐行匹配的 Python 正则表达式")
    path: str = Field(".", title="文件或目录路径")
    glob: str | None = Field(None, title="文件匹配模式", description="可选的文件匹配模式，例如 **/*.py")


class RunShell(Args, title="执行命令"):
    command: str = Field(min_length=1, title="命令")
    timeout: int = Field(60, ge=1, le=3600, title="超时时间", description="允许命令运行的最长时间，单位为秒")
    expected_stdout: str | None = Field(None, title="预期标准输出",
                                        description="短输出的精确核对值；统一换行格式并忽略末尾一个换行。")


_TOOLS = {
    "list_files": (ListFiles, "列出指定目录的直接子项，或按相对于该目录的通配符模式查找文件和目录。"),
    "read_file": (ReadFile, "读取 UTF-8 文本并显示从 1 开始的行号，可指定包含首尾行的范围。"),
    "write_file": (WriteFile, "显示差异供用户确认，然后创建或覆盖 UTF-8 文件。"),
    "edit_file": (EditFile, "按原文精确查找并替换，要求待替换文本只出现一次，其余内容保持原样。"),
    "grep": (Grep, "递归搜索文本文件，返回路径、行号和匹配行的内容。"),
    "run_shell": (RunShell, "在工作区执行终端命令，返回退出码、标准输出和标准错误。"),
}
_IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".uv-cache", ".mini-code-agent"}


def _validation_message(problem):
    field = ".".join(map(str, problem["loc"])) or "参数"
    messages = {
        "json_invalid": "JSON 格式无效",
        "json_type": "必须提供 JSON 文本",
        "model_type": "必须是 JSON 对象",
        "missing": "缺少必填字段",
        "extra_forbidden": "不支持此字段",
        "string_type": "必须是字符串",
        "int_type": "必须是整数",
        "greater_than_equal": "必须大于或等于 {ge}",
        "less_than_equal": "必须小于或等于 {le}",
        "string_too_short": "至少需要 {min_length} 个字符",
    }
    code = problem["type"]
    context = problem.get("ctx", {})
    reason = messages[code].format(**context) if code in messages else problem["msg"]
    if code == "json_invalid":
        position = re.search(r"at line (\d+) column (\d+)", context.get("error", ""))
        if position:
            reason += f"（第 {position[1]} 行，第 {position[2]} 列）"
    return f"{field}：{reason}"


class _Lines:
    def __init__(self):
        self.parts = []
        self.remaining = 12000
        self.count = 0
        self.cut = False

    def add(self, line):
        self.count += 1
        if self.remaining <= 0 or len(self.parts) >= 200:
            self.cut = True
            return
        self.parts.append(line[:self.remaining])
        if len(line) > self.remaining:
            self.cut = True
        self.remaining -= len(self.parts[-1]) + 1

    def render(self, total=None, hint="可缩小目录或匹配范围。"):
        if self.cut:
            self.parts.append(f"[已截断，共 {self.count if total is None else total} 行。{hint}]")
        return "\n".join(self.parts)


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _matches(path, pattern):
    patterns = pattern.replace("\\", "/").split("/")

    @lru_cache(None)
    def match(i, j):
        if j == len(patterns):
            return i == len(path.parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(path.parts) and match(i + 1, j))
        return (i < len(path.parts) and fnmatch.fnmatch(path.parts[i], patterns[j])
                and match(i + 1, j + 1))

    return match(0, 0)


class Tools:
    def __init__(self, root: Path, confirm: Callable[[str], bool], *, config_path: Path | None = None):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("工作区必须是目录")
        self.confirm = confirm
        self.config_path = config_path
        self.schemas = [
            {"name": name, "description": description, "parameters": model.model_json_schema()}
            for name, (model, description) in _TOOLS.items()
        ]

    def execute(self, name: str, arguments: str) -> tuple[str, bool]:
        if name not in _TOOLS:
            return f"未知工具：{name[:200]}", False
        try:
            args = _TOOLS[name][0].model_validate_json(arguments)
            return getattr(self, "_" + name)(**args.model_dump())
        except ValidationError as exc:
            problems = exc.errors(include_input=False, include_url=False)
            message = "；".join(_validation_message(p) for p in problems[:6])
            return f"工具参数无效：{message[:1500]}", False
        except (ValueError, OSError, re.error, RuntimeError, OverflowError) as exc:
            return f"工具执行失败：{str(exc)[:1500]}", False

    def _path(self, relative):
        win = PureWindowsPath(relative)
        parts = Path(relative).parts
        if not relative or win.anchor or ".." in win.parts or any(":" in p for p in parts):
            raise ValueError("请使用工作区内的相对路径，路径中不得包含 '..' 或盘符前缀")
        current = self.root
        for part in parts:
            if os.name == "nt" and (part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()):
                raise ValueError("路径包含 Windows 保留名称或无效的尾随字符")
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if _linked(info):
                raise ValueError("不允许访问符号链接或重解析点")
        resolved = current.resolve()
        if resolved == self.config_path:
            raise ValueError("模型配置文件可能包含密钥，不能通过文件工具访问")
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径超出工作区范围")
        return current

    def _entries(self, start, recursive):
        if not start.is_dir():
            raise ValueError("指定路径必须是目录")
        pending = [start]
        while pending:
            directory = self._path(str(pending.pop().relative_to(self.root)))
            dirs, files = [], []
            for path in sorted(directory.iterdir()):
                info = path.lstat()
                # 先排除链接，再区分目录和文件，避免为判断类型而访问链接目标。
                if _linked(info) or path == self.config_path:
                    continue
                if stat.S_ISDIR(info.st_mode) and path.name not in _IGNORED:
                    dirs.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files.append(path)
            yield from dirs
            yield from files
            if recursive:
                pending.extend(reversed(dirs))

    def _list_files(self, path, pattern):
        start, lines = self._path(path), _Lines()
        for item in self._entries(start, bool(pattern)):
            if pattern is None or _matches(item.relative_to(start), pattern):
                lines.add(item.relative_to(self.root).as_posix() + ("/" if item.is_dir() else ""))
        if not lines.count:
            return ("当前目录没有可列出的条目。" if pattern is None else "没有文件或目录匹配该模式。"), True
        return lines.render(), True

    def _read_file(self, path, start_line, end_line):
        if end_line is not None and end_line < start_line:
            raise ValueError("end_line 不能小于 start_line")
        target, lines = self._path(path), _Lines()
        if not target.is_file():
            raise ValueError("指定路径必须指向已存在的普通文件")
        total = 0
        with target.open(encoding="utf-8-sig") as handle:
            for total, line in enumerate(handle, 1):
                if total >= start_line:
                    lines.add(f"{total}: {line.rstrip(chr(10))}")
                if total == end_line:
                    break
        count = total if end_line is None else lines.count
        return lines.render(count, "可用 start_line 和 end_line 按行号范围继续读取。"), True

    def _grep(self, pattern, path, glob):
        regex, start, lines = re.compile(pattern), self._path(path), _Lines()
        paths = [start] if start.is_file() else self._entries(start, True)
        for item in paths:
            if not item.is_file() or (glob and not _matches(item.relative_to(start if start.is_dir() else start.parent), glob)):
                continue
            with item.open("rb") as stream:
                if b"\x00" in stream.read(2048):
                    continue
                stream.seek(0)
                for number, raw in enumerate(stream, 1):
                    text = raw.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                    if regex.search(text):
                        lines.add(f"{item.relative_to(self.root).as_posix()}:{number}: {text}")
        return lines.render() or "搜索完成，0 处匹配。", True

    def _original(self, target):
        if not target.exists():
            return None
        if not target.is_file():
            raise ValueError("指定路径必须指向普通文件")
        return target.read_bytes()

    def _replace(self, path, original, content):
        old = (original or b"").decode("utf-8-sig")
        # 编辑时保留匹配区域之外的原有换行符，写入时沿用原文件的字节序标记。
        preview = difflib.unified_diff(old.splitlines(keepends=True), content.splitlines(keepends=True),
                                       fromfile=path if original is not None else "/dev/null", tofile=path)
        prompt = f"写入文件 {path}\n" + "\n".join(line.rstrip("\n") for line in preview)
        if not self.confirm(prompt):
            return "用户拒绝了此次操作", False
        target = self._path(path)
        if self._original(target) != original:
            raise ValueError("预览后文件已更改，请重新读取后重试")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8-sig" if (original or b"").startswith(b"\xef\xbb\xbf") else "utf-8")
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
            try:
                tmp.write(data)
                tmp.close()
                if original is not None:
                    temp_path.chmod(stat.S_IMODE(target.stat().st_mode))
                os.replace(temp_path, target)
            finally:
                tmp.close()
                temp_path.unlink(missing_ok=True)
        return f"已写入 {path}"[:1500], True

    def _write_file(self, path, content):
        return self._replace(path, self._original(self._path(path)), content)

    def _edit_file(self, path, old_string, new_string):
        original = self._original(self._path(path))
        if original is None:
            raise ValueError("文件不存在")
        text = original.decode("utf-8-sig")
        index = text.find(old_string)
        if index < 0:
            raise ValueError("未找到 old_string 指定的文本")
        if text.find(old_string, index + 1) >= 0:
            raise ValueError("old_string 匹配了多个位置")
        return self._replace(path, original, text.replace(old_string, new_string, 1))

    def _run_shell(self, command, timeout, expected_stdout=None):
        if not self.confirm(f"在 {self.root} 中执行命令\n{command}"):
            return "用户拒绝了此次操作", False
        return run(command, self.root, timeout, expected_stdout)
