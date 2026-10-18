# coding: utf-8
"""
服务端守护模块

把客户端与服务端绑定成"一起启动、一起关闭"，避免只开一边的情况：

- 客户端启动时若发现服务端没在跑，自动拉起 start_server.exe
- 客户端退出（关窗口 / Ctrl+C / 托盘退出）时，一并关闭服务端
- 运行中若服务端被手动关闭，客户端也自动退出，不留半死不活的状态

不额外开任何窗口：服务端作为客户端的子进程启动，日志直接混在同一个控制台里。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from config_client import BASE_DIR, ClientConfig as Config
from core.client.state import console
from . import logger


# Windows 控制台事件
CTRL_C_EVENT = 0
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

_CHECK_INTERVAL = 3.0        # 巡检间隔（秒）
_MISS_THRESHOLD = 3          # 连续几次判定服务端不在才动手，避免抖动误判
_STARTUP_WAIT = 30.0         # 启动后等待服务端就绪的最长时间（秒）

_TASKKILL = str(
    Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
)


class ServerGuard:
    """
    服务端守护器

    职责：
    1. ensure_server()：确保服务端进程与端口可用，缺失则拉起
    2. 后台线程巡检：服务端消失则带着客户端一起退出
    3. stop()：关闭服务端进程，并挂上控制台关闭钩子
    """

    def __init__(self, app):
        self.app = app
        self.port = int(Config.port)
        self.exe_name = 'start_server.exe'
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stopped = False
        # 必须保持引用，否则会被 GC 回收导致 ctypes 回调悬空
        self._ctrl_handler = None

    # ---------- 内部工具 ----------

    def _port_open(self) -> bool:
        """探测服务端端口是否可连接"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                return sock.connect_ex(('127.0.0.1', self.port)) == 0
        except OSError:
            return False

    def _kill_server(self) -> None:
        """结束服务端进程树"""
        try:
            subprocess.run(
                [_TASKKILL, '/IM', self.exe_name, '/T', '/F'],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            logger.debug(f"关闭服务端进程时发生错误: {e}")

    # ---------- 对外接口 ----------

    def start(self) -> None:
        """客户端启动时调用：确保服务端在跑，然后开始巡检"""
        self.ensure_server()
        self._install_console_hook()
        self._thread = threading.Thread(target=self._watch, name='ServerGuard', daemon=True)
        self._thread.start()
        logger.info("服务端守护已启用（缺服务端自动拉起，关任意一方另一方一起关）")

    def ensure_server(self) -> bool:
        """若服务端未运行则自动启动，并等待端口就绪"""
        if self._port_open():
            logger.info(f"服务端已在运行（端口 {self.port}）")
            return True

        exe = Path(BASE_DIR) / self.exe_name
        if not exe.exists():
            logger.warning(f"未找到服务端程序 {exe}，无法自动启动")
            return False

        logger.info("未检测到运行中的服务端，正在自动启动...")
        console.print('[yellow]未检测到服务端，正在自动启动，请稍候...[/yellow]\n')
        try:
            # CREATE_NEW_PROCESS_GROUP: 让 Ctrl+C 只作用于客户端，不误杀服务端
            subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as e:
            logger.error(f"自动启动服务端失败: {e}")
            return False

        deadline = time.time() + _STARTUP_WAIT
        while time.time() < deadline:
            if self._port_open():
                logger.info(f"服务端已就绪（端口 {self.port}）")
                return True
            time.sleep(1.0)

        logger.warning("等待服务端就绪超时，客户端将持续尝试重连")
        return False

    def stop(self) -> None:
        """客户端退出时调用：一并关闭服务端"""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        logger.info("正在一并关闭服务端...")
        self._kill_server()

    # ---------- 巡检 ----------

    def _watch(self) -> None:
        """服务端消失超过阈值即带着客户端一起退出"""
        missed = 0
        while not self._stop_event.is_set():
            if self._stop_event.wait(_CHECK_INTERVAL):
                break
            if self._port_open():
                missed = 0
                continue
            missed += 1
            if missed < _MISS_THRESHOLD:
                continue
            logger.warning("检测到服务端已关闭，客户端一并退出")
            console.print('[bold red]服务端已关闭，客户端一并退出[/bold red]\n')
            self._kill_server()
            os._exit(0)

    # ---------- 控制台关闭钩子 ----------

    def _install_console_hook(self) -> None:
        """
        捕获控制台窗口关闭事件。

        信号处理器里只注册了 SIGINT，也就是按 X 关窗口时 stop() 根本不会被调用，
        服务端会变成孤儿进程。这里补上关闭事件的处理。
        """
        if not sys.platform.startswith('win'):
            return

        PROC_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        def _handler(event):
            # 不拦 CTRL_C_EVENT：信号处理器里已有"按两次才退出"的防误触逻辑
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                self._kill_server()
                os._exit(0)
            return True

        self._ctrl_handler = PROC_ROUTINE(_handler)
        kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)
