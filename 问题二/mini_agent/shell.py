"""执行命令，限制捕获的输出大小，并管理整个子进程树。"""

import ctypes
import locale
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


class _Capture:
    def __init__(self):
        self.prefix = bytearray()
        self.size = self.newlines = 0
        self.last = b""

    def drain(self, stream):
        with stream:
            while chunk := stream.read(65536):
                self.size += len(chunk)
                self.newlines += chunk.count(b"\n")
                self.last = chunk[-1:]
                self.prefix.extend(chunk[: 6000 - len(self.prefix)])

    def render(self):
        try:
            text = self.prefix.decode("utf-8")
        except UnicodeDecodeError:
            text = self.prefix.decode(locale.getpreferredencoding(False), errors="replace")
        if self.size > len(self.prefix):
            lines = self.newlines + int(self.last != b"\n")
            text += f"\n[已截断，共 {lines} 行。可将输出重定向到工作目录文件并按行读取。]"
        return text


class _WindowsJob:
    """将等待中的启动进程加入作业对象，再允许它启动命令解释器和子进程。"""

    def __init__(self):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        try:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(self.handle)


def run(command: str, root: Path, timeout: int) -> tuple[str, bool]:
    windows = os.name == "nt"
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("API_KEY")}
    job = _WindowsJob() if windows else None
    # Windows 启动进程收到放行字节后才会创建子进程，确保整个进程树都受作业对象管理。
    launcher = (
        "import subprocess,sys; "
        "sys.exit(subprocess.call(sys.argv[1], shell=True, stdin=subprocess.DEVNULL) "
        "if sys.stdin.buffer.read(1) == b'1' else 1)"
    )
    streams = [_Capture(), _Capture()]
    workers = []
    process = None
    timed_out = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", launcher, command] if windows else command,
            shell=not windows, cwd=root, env=env,
            stdin=subprocess.PIPE if windows else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=not windows,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        )
        if job:
            job.assign(process)
            process.stdin.write(b"1")
            process.stdin.close()
        for capture, stream in zip(streams, (process.stdout, process.stderr)):
            worker = threading.Thread(target=capture.drain, args=(stream,), daemon=True)
            worker.start()
            workers.append(worker)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        try:
            if job:
                job.close()
            elif process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if process:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    process.kill()
                process.wait()
                for worker in workers:
                    worker.join()
                process.stdout.close()
                process.stderr.close()
    status = f"命令执行超时（{timeout} 秒）\n" if timed_out else ""
    result = (f"{status}退出码：{process.returncode}\n标准输出：\n{streams[0].render()}"
              f"\n标准错误：\n{streams[1].render()}")
    return result, not timed_out and process.returncode == 0
