"""提供有界文件访问、显式写入路径校验和工作区内容指纹。"""

import hashlib
import difflib
import errno
import fnmatch
import json
import os
import selectors
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from uuid import uuid4


class SandboxViolation(ValueError):
    """请求违反路径、敏感目录、文件类型或读取大小约束。"""


class Sandbox:
    """基于路径解析的文件边界。

    先校验请求与真实落点，再从固定工作区逐级打开禁止软链接的目录描述符。
    实际文件访问和替换使用描述符，不依赖校验后再次解析绝对路径。
    不提供操作系统级进程隔离。
    """

    def __init__(self, workspace: Path, max_read_bytes: int = 64 * 1024):
        """绑定已存在的工作区目录，默认限制每次读取最多 64 KiB 原始内容。"""
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        if max_read_bytes < 1:
            raise ValueError("max_read_bytes must be positive")
        self.max_read_bytes = max_read_bytes
        metadata = self.workspace.stat()
        self._workspace_identity = (metadata.st_dev, metadata.st_ino)

    @contextmanager
    def _open_directory(self, directory: Path):
        """从工作区根逐级打开目录，拒绝根被替换及任意中间层软链接。"""
        try:
            parts = directory.relative_to(self.workspace).parts
        except ValueError:
            raise SandboxViolation("Directory escapes the workspace") from None
        if ".." in parts:
            raise SandboxViolation("Parent traversal is not allowed")
        self._check_sensitive(parts)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = None
        try:
            descriptor = os.open(self.workspace, flags)
            root = os.fstat(descriptor)
            if (root.st_dev, root.st_ino) != self._workspace_identity:
                raise SandboxViolation("Workspace root changed after initialization")
            for part in parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise SandboxViolation("Path changed or contains a symlink") from error
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _open_regular(self, parent: int, name: str):
        """相对已打开的父目录读取普通文件；非阻塞打开避免替换成 FIFO 后挂起。"""
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SandboxViolation("Only regular files may be read")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                yield stream, metadata
        finally:
            os.close(descriptor)

    def _read_resolved(self, resolved: Path, limit: int) -> bytes:
        """从校验后的工作区相对位置读取有界字节，实际打开时仍禁止软链接。"""
        with self._open_directory(resolved.parent) as parent:
            with self._open_regular(parent, resolved.name) as (stream, _):
                return stream.read(limit)

    def resolve_read(self, path: str) -> Path:
        """验证工作区相对路径，返回已存在普通文件的绝对路径。

        在实际 handler 执行前可作为 Gate 的 preflight 使用。越界、敏感路径
        和非普通文件抛出 SandboxViolation；目标不存在时传播文件系统异常。
        """
        requested = Path(path)
        if requested.is_absolute():
            raise SandboxViolation("Only workspace-relative paths are allowed")
        self._check_sensitive(requested.parts)
        # strict=True 解析真实落点，随后用路径层级关系检查，而非字符串前缀。
        resolved = (self.workspace / requested).resolve(strict=True)
        if not resolved.is_relative_to(self.workspace):
            raise SandboxViolation("Path escapes the workspace")
        # 防止普通文件名经软链接指向工作区内的敏感文件。
        self._check_sensitive(resolved.relative_to(self.workspace).parts)
        if not resolved.is_file():
            raise SandboxViolation("Only regular files may be read")
        return resolved

    @staticmethod
    def _check_sensitive(parts: tuple[str, ...]) -> None:
        """拒绝任意路径层级中的敏感目录、.env 及 .env.* 文件。"""
        sensitive = {".git", ".ssh", ".aws", ".codex", ".agents", ".venv", "__pycache__", ".pytest_cache"}
        if any(part in sensitive or part == ".env" or part.startswith(".env.") for part in parts):
            raise SandboxViolation("Sensitive path is not allowed")

    def read_file(self, path: str) -> dict:
        """返回 path、sha256 和 UTF-8 content，哈希基于实际读取的原始字节。

        只读取上限加一个字节以判断超限，避免先将大文件整体加载到内存。
        非 UTF-8 内容抛出 UnicodeDecodeError，由 Gate 转换为执行错误。
        限制针对文件原始内容，不包含 JSON 编码后的额外开销。
        """
        # 执行时重新校验路径，不依赖此前 preflight 的解析结果。
        resolved = self.resolve_read(path)
        data = self._read_resolved(resolved, self.max_read_bytes + 1)
        if len(data) > self.max_read_bytes:
            raise SandboxViolation("File exceeds the read/output limit")
        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "content": data.decode("utf-8"),
        }

    def resolve_directory(self, path: str) -> Path:
        """解析工作区内目录，拒绝绝对路径、敏感路径和软链接越界。"""
        requested = Path(path)
        if requested.is_absolute():
            raise SandboxViolation("Only workspace-relative directories are allowed")
        self._check_sensitive(requested.parts)
        directory = (self.workspace / requested).resolve(strict=True)
        if not directory.is_relative_to(self.workspace):
            raise SandboxViolation("Directory escapes the workspace")
        self._check_sensitive(directory.relative_to(self.workspace).parts)
        if not directory.is_dir():
            raise SandboxViolation("Directory is required")
        return directory

    def validate_read_range(self, path: str, start_line: int, end_line: int) -> Path:
        """读取范围以 1 起始且最多 2000 行，路径检查在 handler 前完成。"""
        if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line or end_line - start_line >= 2000:
            raise SandboxViolation("Line range must start at 1 or greater and contain at most 2000 lines")
        return self.resolve_read(path)

    def _source_bytes(self, path: str) -> bytes:
        """读取最多 8 MiB 的固定文件快照，用于片段哈希与最终证据复核。"""
        data = self._read_resolved(self.resolve_read(path), 8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise SandboxViolation("Source snapshot exceeds 8 MiB")
        return data

    def file_sha256(self, path: str) -> str:
        """核对完整文件内容，不把完整文件加入模型上下文。"""
        return hashlib.sha256(self._source_bytes(path)).hexdigest()

    def read_range(self, path: str, start_line: int, end_line: int) -> dict:
        """返回有界行片段、行范围及完整文件哈希；片段不代表读取了其他行。"""
        self.validate_read_range(path, start_line, end_line)
        data = self._source_bytes(path)
        lines = data.decode("utf-8").splitlines(keepends=True)
        if start_line > len(lines):
            raise SandboxViolation("start_line exceeds the file line count")
        content = "".join(lines[start_line - 1:end_line])
        if len(content.encode("utf-8")) > self.max_read_bytes:
            raise SandboxViolation("Line range exceeds the read/output limit")
        return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "content": content,
                "start_line": start_line, "end_line": min(end_line, len(lines)), "total_lines": len(lines), "partial": True}

    def _walk_files(self, directory: Path, stats: dict):
        """有界遍历普通文件，不跟随软链接；最多检查 2000 个条目、20 层目录。"""
        pending = [(directory, 0)]
        while pending:
            parent, depth = pending.pop()
            try:
                with self._open_directory(parent) as descriptor, os.scandir(descriptor) as entries:
                    for entry in entries:
                        if stats["scanned"] >= 2000:
                            stats["truncated"] = True
                            return
                        stats["scanned"] += 1
                        try:
                            self._check_sensitive((entry.name,))
                        except SandboxViolation:
                            continue
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth < 20:
                                pending.append((parent / entry.name, depth + 1))
                            else:
                                stats["truncated"] = True
                        elif entry.is_file(follow_symlinks=False):
                            yield parent / entry.name
            except PermissionError:
                stats["skipped_directories"] += 1

    def _fits_discovery_output(self, items: list[dict]) -> bool:
        """限制发现结果主体的 JSON 字节数，预留元数据空间。"""
        return len(json.dumps(items, ensure_ascii=False).encode("utf-8")) <= max(0, self.max_read_bytes - 1024)

    def validate_list_files(self, path: str) -> Path:
        """校验目录真实落点及根身份，不枚举内容；Gate 与实际执行共用此边界。"""
        directory = self.resolve_directory(path)
        with self._open_directory(directory):
            pass
        return directory

    def validate_find_files(self, pattern: str) -> None:
        """校验有界相对 glob 及工作区根，不扫描仓库。"""
        if not pattern or len(pattern.encode("utf-8")) > 256 or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise SandboxViolation("A bounded workspace-relative glob pattern is required")
        with self._open_directory(self.workspace):
            pass

    def validate_search_code(self, query: str, path: str) -> Path:
        """校验搜索词及目录边界，不读取文件；执行时再次核对。"""
        if not query or len(query.encode("utf-8")) > 256:
            raise SandboxViolation("Search query must contain 1 to 256 UTF-8 bytes")
        return self.validate_list_files(path)

    def list_files(self, path: str) -> dict:
        """列出目录的直接子项，隐藏敏感条目及软链接，最多返回 200 项。"""
        directory = self.validate_list_files(path)
        result = {"entries": [], "scanned": 0, "truncated": False}
        with self._open_directory(directory) as descriptor, os.scandir(descriptor) as entries:
            for entry in entries:
                if result["scanned"] >= 2000:
                    result["truncated"] = True
                    break
                result["scanned"] += 1
                try:
                    self._check_sensitive((entry.name,))
                except SandboxViolation:
                    continue
                if entry.is_symlink():
                    continue
                kind = "directory" if entry.is_dir(follow_symlinks=False) else "file" if entry.is_file(follow_symlinks=False) else None
                if kind is None:
                    continue
                item = {"path": (directory / entry.name).relative_to(self.workspace).as_posix(), "type": kind}
                if len(result["entries"]) >= 200 or not self._fits_discovery_output(result["entries"] + [item]):
                    result["truncated"] = True
                    break
                result["entries"].append(item)
        result["entries"].sort(key=lambda item: item["path"])
        return result

    def find_files(self, pattern: str) -> dict:
        """按工作区相对路径 glob 查找文件，最多返回 200 项并显式标记截断。"""
        self.validate_find_files(pattern)
        stats = {"scanned": 0, "truncated": False, "skipped_directories": 0}
        files = []
        for path in self._walk_files(self.workspace, stats):
            relative = path.relative_to(self.workspace).as_posix()
            if fnmatch.fnmatchcase(relative, pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:])):
                item = {"path": relative}
                if len(files) >= 200 or not self._fits_discovery_output(files + [item]):
                    stats["truncated"] = True
                    break
                files.append(item)
        return {"files": sorted(files, key=lambda item: item["path"]), **stats}

    def search_code(self, query: str, path: str) -> dict:
        """对目录内 UTF-8 文件执行字面量搜索，不执行正则表达式。

        最多读取 1 MiB 内容、返回 100 个匹配，每行截取 300 字符；单文件仍受
        read_file 大小及路径限制。搜索摘要不能代替完成检查所需的完整读取证据。
        """
        directory = self.validate_search_code(query, path)
        stats = {"scanned": 0, "truncated": False, "skipped_directories": 0}
        matches = []
        bytes_read = 0
        stop = False
        for candidate in self._walk_files(directory, stats):
            if bytes_read >= 1024 * 1024:
                stats["truncated"] = True
                break
            relative = candidate.relative_to(self.workspace).as_posix()
            try:
                resolved = self.resolve_read(relative)
                remaining = 1024 * 1024 - bytes_read
                raw = self._read_resolved(resolved, min(self.max_read_bytes + 1, remaining))
                bytes_read += len(raw)
                if len(raw) > self.max_read_bytes:
                    continue
                content = raw.decode("utf-8")
                if "\0" in content:
                    continue
                if len(raw) == remaining:
                    stats["truncated"] = True
            except (SandboxViolation, OSError, UnicodeError):
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if query not in line:
                    continue
                item = {"path": relative, "line": number, "text": line[:300]}
                if len(matches) >= 100 or not self._fits_discovery_output(matches + [item]):
                    stats["truncated"] = True
                    stop = True
                    break
                matches.append(item)
            if stop:
                break
        return {"matches": matches, "bytes_read": bytes_read, **stats}

    def validate_write_directory(self, path: str) -> Path:
        """校验宿主开放的目录范围，禁止工作区根、敏感目录及链接目录。"""
        requested = Path(path)
        if path in {"", "."} or requested.is_absolute() or ".." in requested.parts or requested.as_posix() != path:
            raise SandboxViolation("Write directory must be a normalized relative subdirectory")
        self._check_sensitive(requested.parts)
        candidate = self.workspace
        for part in requested.parts:
            candidate /= part
            if candidate.is_symlink():
                raise SandboxViolation("Symlinks are not allowed in write directories")
        return self.validate_list_files(path)

    def validate_write(self, path: str, content: str, writable_paths: list[str], *, directories=()) -> Path:
        """验证精确写入白名单、内容大小和目标路径，返回允许替换的文件路径。

        白名单使用工作区相对 POSIX 路径，不接受通配符。禁止路径中的 ..、绝对路径
        和任意层级软链接；允许创建新文件，但父目录必须已存在。
        """
        requested = Path(path)
        if requested.is_absolute() or ".." in requested.parts or requested.as_posix() != path:
            raise SandboxViolation("Write path must be a normalized workspace-relative path")
        self._check_sensitive(requested.parts)
        permitted = path in writable_paths
        for directory in directories:
            allowed = self.validate_write_directory(directory)
            if (self.workspace / requested).is_relative_to(allowed):
                permitted = True
        if not permitted:
            raise SandboxViolation("Write path is not explicitly allowed")
        if len(content.encode("utf-8")) > self.max_read_bytes:
            raise SandboxViolation("Write content exceeds the size limit")
        candidate = self.workspace
        for part in requested.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise SandboxViolation("Symlinks are not allowed in write paths")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.workspace) or not resolved.parent.is_dir():
            raise SandboxViolation("Write target or its parent is outside the allowed boundary")
        if resolved.exists() and not resolved.is_file():
            raise SandboxViolation("Write target must be a regular file")
        return resolved

    def write_file(self, path: str, content: str, writable_paths: list[str], *, directories=(), expected_sha256: str | None = None) -> dict:
        """在固定父目录描述符内原子替换，保留原文件权限并返回前后哈希。

        该方法负责文件边界；审批与写入意图持久化由 Gate 保证。
        """
        target = self.validate_write(path, content, writable_paths, directories=directories)
        data = content.encode("utf-8")
        previous = None
        mode = 0o600
        with self._open_directory(target.parent) as parent:
            try:
                with self._open_regular(parent, target.name) as (stream, metadata):
                    previous = stream.read(self.max_read_bytes + 1)
                    mode = stat.S_IMODE(metadata.st_mode)
            except FileNotFoundError:
                pass
            if previous is not None and len(previous) > self.max_read_bytes:
                raise SandboxViolation("File exceeds the read/output limit")
            if expected_sha256 is not None and (previous is None or hashlib.sha256(previous).hexdigest() != expected_sha256):
                raise SandboxViolation("Edit base hash changed before publication")
            previous_text = previous.decode("utf-8") if previous is not None else ""
            temporary = ".guarded-" + uuid4().hex + ".tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                # 若父目录在生成临时文件期间已被移走或替换，停止发布；清理仍绑定原目录。
                with self._open_directory(target.parent) as current:
                    pinned, present = os.fstat(parent), os.fstat(current)
                    if (pinned.st_dev, pinned.st_ino) != (present.st_dev, present.st_ino):
                        raise SandboxViolation("Write parent changed during execution")
                try:
                    if stat.S_ISLNK(os.stat(target.name, dir_fd=parent, follow_symlinks=False).st_mode):
                        raise SandboxViolation("Symlinks are not allowed in write paths")
                except FileNotFoundError:
                    pass
                os.replace(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        before = hashlib.sha256(previous).hexdigest() if previous is not None else None
        diff = "".join(difflib.unified_diff(
            previous_text.splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=f"a/{path}" if previous is not None else "/dev/null", tofile=f"b/{path}",
        ))
        return {"path": path, "before_sha256": before, "sha256": hashlib.sha256(data).hexdigest(), "diff": diff}

    def validate_test(self, name: str, commands: dict[str, list[str]]) -> list[str]:
        """只接受宿主注册的命令别名，校验固定 argv 和绝对可执行文件路径。

        模型不能追加参数、提供环境变量或指定工作目录。命令配置属于宿主信任边界，
        argv 以列表形式传给 Popen，不经过 shell 展开。
        """
        argv = commands.get(name)
        if not isinstance(argv, list) or not argv or any(type(item) is not str or "\0" in item for item in argv):
            raise SandboxViolation("Test command is not configured with a valid argv")
        executable = Path(argv[0])
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise SandboxViolation("Test executable must be an existing absolute executable path")
        return list(argv)

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        """先发送 SIGTERM，再向整个进程组发送 SIGKILL，最后回收直接子进程。

        即使组长已经退出，也清理仍属于同一组的派生进程。无法清理的异常向上传播。
        """
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(0.1)
        # 先回收已经退出的组长，避免对仅剩僵尸组长的组重复发送终止信号。
        process.poll()
        try:
            os.killpg(process.pid, 0)
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def run_test(
        self, name: str, commands: dict[str, list[str]], runtime_dir: Path,
        timeout_seconds: float = 30.0, output_limit: int = 64 * 1024,
        cancel: Event | None = None, on_start=None,
    ) -> dict:
        """执行配置中的测试命令，返回退出码、终止原因及有界合并输出。

        创建独立会话和进程组，超时、取消、输出超限或异常时终止整个组。
        环境从固定字典构造，不继承 API Key、代理、PYTHONPATH 或 pytest 注入参数。
        该边界不隔离仓库代码的文件系统、网络或主动脱离进程组的行为。
        """
        argv = self.validate_test(name, commands)
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 3600:
            raise ValueError("Test timeout must be in (0, 3600] seconds")
        if type(output_limit) is not int or output_limit < 1:
            raise ValueError("Test output limit must be positive")
        runtime_dir = Path(runtime_dir).resolve()
        if runtime_dir == self.workspace or runtime_dir.is_relative_to(self.workspace):
            raise SandboxViolation("Test runtime directory must be outside the workspace")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": str(Path(argv[0]).parent) + os.pathsep + os.defpath,
            "HOME": str(runtime_dir), "TMPDIR": str(runtime_dir),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        started = time.monotonic()
        process = subprocess.Popen(
            argv, cwd=self.workspace, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, start_new_session=True,
        )
        output = bytearray()
        status = None
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            if on_start is not None:
                on_start(process.pid)
            while True:
                if cancel is not None and cancel.is_set():
                    status = "CANCELLED"
                    break
                if time.monotonic() - started >= timeout_seconds:
                    status = "TIMEOUT"
                    break
                events = selector.select(timeout=0.02)
                if events:
                    chunk = os.read(process.stdout.fileno(), 16 * 1024)
                    if not chunk:
                        selector.unregister(process.stdout)
                        if process.poll() is not None:
                            break
                    else:
                        available = output_limit - len(output)
                        output.extend(chunk[:available])
                        if len(chunk) > available:
                            status = "OUTPUT_LIMIT"
                            break
                elif process.poll() is not None:
                    # 组长退出且管道暂无可读数据时结束读取；派生进程可能仍持有管道，
                    # 不能为等待其 EOF 而遗留后台进程，finally 会清理整个组。
                    break
        finally:
            selector.close()
            try:
                self._kill_group(process)
            finally:
                process.stdout.close()
        if status is None:
            status = "PASSED" if process.returncode == 0 else "FAILED"
        return {
            "name": name, "argv": argv, "status": status, "returncode": process.returncode,
            "output": output.decode("utf-8", errors="replace"), "output_bytes": len(output),
            "duration_seconds": round(time.monotonic() - started, 6), "process_group": process.pid,
        }

    def fingerprint(self) -> str:
        """计算可访问工作区的路径、文件内容和软链接目标指纹。

        排除敏感目录、虚拟环境及 Python/pytest 缓存；不跟随软链接，也不读取外部落点。
        文件逐块哈希，避免大文件整体加载；模式变化和文件新增、删除也参与版本校验。
        """
        records = []
        ignored = {".git", ".ssh", ".aws", ".codex", ".agents", ".venv", "__pycache__", ".pytest_cache"}
        def visit(parent: int, prefix: Path) -> None:
            """以已打开目录遍历，不通过被并发替换的路径读取文件或子目录。"""
            for name in sorted(os.listdir(parent)):
                if name in ignored or name == ".env" or name.startswith(".env."):
                    continue
                relative = (prefix / name).as_posix()
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    records.append([relative, "symlink", os.readlink(name, dir_fd=parent)])
                elif stat.S_ISDIR(metadata.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                    try:
                        visit(child, prefix / name)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(metadata.st_mode):
                    digest = hashlib.sha256()
                    with self._open_regular(parent, name) as (stream, opened):
                        for chunk in iter(lambda: stream.read(64 * 1024), b""):
                            digest.update(chunk)
                    records.append([relative, stat.S_IMODE(opened.st_mode), digest.hexdigest()])
                else:
                    records.append([relative, "special", metadata.st_mode])
        with self._open_directory(self.workspace) as root:
            visit(root, Path())
        return hashlib.sha256(json.dumps(sorted(records), ensure_ascii=False).encode("utf-8")).hexdigest()
