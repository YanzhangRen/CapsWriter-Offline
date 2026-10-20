# coding: utf-8
"""
快捷键管理器（重构版）

统一管理多个快捷键，处理键盘和鼠标事件，支持：
1. 多快捷键并发处理
2. 防止不同按键互相干扰
3. restore 功能的防自捕获逻辑
4. hold_mode 和 click_mode 支持
"""
from __future__ import annotations
import ctypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional

from pynput import keyboard, mouse

from . import logger
from core.client.shortcut.key_mapper import *
from core.client.shortcut.key_mapper import KeyMapper
from core.client.shortcut.emulator import ShortcutEmulator
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.task import ShortcutTask

if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient


_user32 = ctypes.windll.user32

# 前台窗口轮询间隔（秒）
_FOREGROUND_POLL_INTERVAL = 0.15
# 两次重装钩子的最小间隔（秒），只用来防止窗口来回抖时反复重装
_REINSTALL_COOLDOWN = 0.5
# 按键状态残留多久判定为卡死（秒）
_STALE_KEY_STATE_TIMEOUT = 3.0



class ShortcutManager:
    """
    快捷键管理器

    统一管理多个快捷键，使用 pynput 监听键盘和鼠标事件。
    所有事件处理都在 win32_event_filter 中完成，确保高性能和低延迟。
    """

    def __init__(self, app: CapsWriterClient, shortcuts: List[Shortcut]):
        """
        初始化快捷键管理器

        Args:
            app: 客户端 App 实例
            shortcuts: 快捷键配置列表
        """
        self.app = app
        self.shortcuts = shortcuts

        # 监听器
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None

        # 快捷键任务映射（key -> ShortcutTask）
        self.tasks: Dict[str, ShortcutTask] = {}

        # ---- 组合键支持 ----
        # _combo_index: 组成键名 -> 参与该组合的快捷键字符串列表
        #   例如 {'ctrl_l': ['ctrl_l+cmd_l', 'ctrl_l+alt_l']}
        self._combo_index: Dict[str, List[str]] = {}
        # _pressed_keys: 当前处于按下状态的按键名集合
        self._pressed_keys = set()
        # _combo_pressed: 组合键 -> 本次按下周期是否已触发
        #   防止按住不放时，持续的 KEYDOWN_REPEAT 重复触发
        self._combo_pressed: Dict[str, bool] = {}

        # 线程池
        self._pool = ThreadPoolExecutor(max_workers=4)

        # 钩子守护线程：前台窗口切换时重新安装钩子
        self._hook_watch_stop = threading.Event()
        self._hook_watch_thread: Optional[threading.Thread] = None

        # 按键模拟器
        self._emulator = ShortcutEmulator()

        # 按键恢复状态追踪
        self._restoring_keys = set()

        # 事件处理器
        self._event_handler = ShortcutEventHandler(self.tasks, self._pool, self._emulator)

        # 初始化快捷键任务
        self._init_tasks()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _init_tasks(self) -> None:
        """初始化所有快捷键任务"""
        from config_client import ClientConfig as Config

        for shortcut in self.shortcuts:
            if not shortcut.enabled:
                continue

            task = ShortcutTask(self.app, shortcut)
            task._manager_ref = lambda: self  # 弱引用，用于回调
            task.pool = self._pool
            task.threshold = shortcut.get_threshold(Config.threshold)
            self.tasks[shortcut.key] = task

            # 组合键（形如 'ctrl_l+cmd_l'）建立反向索引，
            # 使任意一个组成键按下时都能反查到所属组合
            if '+' in shortcut.key:
                for k in shortcut.key.split('+'):
                    k = k.strip()
                    if k:
                        self._combo_index.setdefault(k, []).append(shortcut.key)

    @staticmethod
    def _combo_keys(combo: str) -> List[str]:
        """拆分组合键字符串为组成键列表"""
        return [k.strip() for k in combo.split('+') if k.strip()]

    def _match_combos(self, key_name: str, is_down: bool) -> bool:
        """
        匹配并派发组合键事件

        Args:
            key_name: 当前事件的按键名
            is_down: True=按下事件, False=释放事件

        Returns:
            True 表示该按键参与了某个组合键（已由组合逻辑处理）

        触发条件：组合键的所有组成键都处于按下状态时，在最后一个键按下
        的那一刻触发；任意组成键释放时，视为快捷键释放。
        """
        combos = self._combo_index.get(key_name)
        if not combos:
            return False

        handled = False
        for combo in combos:
            task = self.tasks.get(combo)
            if task is None:
                continue
            handled = True

            if is_down:
                self._pressed_keys.add(key_name)
                keys = self._combo_keys(combo)
                if all(k in self._pressed_keys for k in keys):
                    # 防止按住不放时 KEYDOWN_REPEAT 重复触发
                    if not self._combo_pressed.get(combo):
                        self._combo_pressed[combo] = True
                        self._event_handler.handle_keydown(combo, task)
            else:
                self._pressed_keys.discard(key_name)
                # 任意组成键释放即视为释放。重复释放由 handle_keyup
                # 内部的 is_recording / pressed 判空保护，不会重复结束录音。
                if self._combo_pressed.get(combo):
                    self._combo_pressed[combo] = False
                    self._event_handler.handle_keyup(combo, task)

        return handled

    # ========== 监听器创建 ==========

    def create_keyboard_filter(self):
        """创建键盘事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 KEYDOWN 和 KEYUP 消息
            if msg not in KEYBOARD_MESSAGES:
                return True

            key_name = KeyMapper.vk_to_name(data.vkCode)

            # 防自捕获检查
            if self._check_emulating(key_name, msg):
                return True
            if self._check_restoring(key_name, msg):
                return True

            # 查找匹配的快捷键
            # 先尝试组合键（如 ctrl_l+cmd_l）：参与组合的按键优先由组合逻辑处理
            if self._match_combos(key_name, msg in KEY_DOWN_MESSAGES):
                return True

            if key_name not in self.tasks:
                return True

            task = self.tasks[key_name]

            # 处理按键事件
            if msg in KEY_DOWN_MESSAGES:
                self._event_handler.handle_keydown(key_name, task)
            elif msg in KEY_UP_MESSAGES:
                self._event_handler.handle_keyup(key_name, task)

            # 阻塞事件
            if task.shortcut.suppress and self.keyboard_listener:
                self.keyboard_listener.suppress_event()

            return True

        return win32_event_filter

    def create_mouse_filter(self):
        """创建鼠标事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 XBUTTON 消息
            if msg not in MOUSE_MESSAGES:
                return True

            # 获取按键标识
            xbutton = (data.mouseData >> 16) & 0xFFFF
            button_name = 'x1' if xbutton == XBUTTON1 else 'x2'

            # 防自捕获检查
            if self._check_emulating(button_name, msg, is_mouse=True):
                return True

            # 查找匹配的快捷键
            if button_name not in self.tasks:
                return True

            task = self.tasks[button_name]

            # 处理鼠标事件
            if msg == WM_XBUTTONDOWN:
                self._event_handler.handle_keydown(button_name, task)
            elif msg == WM_XBUTTONUP:
                self._handle_mouse_keyup(button_name, task)

            # 阻塞事件
            if task.shortcut.suppress and self.mouse_listener:
                self.mouse_listener.suppress_event()

            return True

        return win32_event_filter

    def _handle_mouse_keyup(self, button_name: str, task) -> None:
        """处理鼠标按键释放事件"""
        # 单击模式
        if not task.shortcut.hold_mode:
            if task.pressed:
                task.pressed = False
                task.released = True
                task.event.set()
            return

        # 长按模式
        if not task.is_recording:
            return

        duration = time.time() - task.recording_start_time
        logger.debug(f"[{button_name}] 松开按键，持续时间: {duration:.3f}s")

        if duration < task.threshold:
            task.cancel()
            if task.shortcut.suppress:
                logger.debug(f"[{button_name}] 安排异步补发鼠标按键")
                self._pool.submit(self._emulator.emulate_mouse_click, button_name)
        else:
            task.finish()

    # ========== 按键恢复管理 ==========

    def schedule_restore(self, key: str) -> None:
        """
        安排按键恢复（延迟执行，避免在事件处理中阻塞）

        Args:
            key: 要恢复的按键

        注意：标志清除只在按键释放事件中处理（_check_restoring），
        避免在线程中提前清除导致主线程收到重复消息。
        """
        from pynput import keyboard

        self._restoring_keys.add(key)

        def do_restore():
            import time
            time.sleep(0.05)  # 延迟 50ms
            if key == 'caps_lock':
                controller = keyboard.Controller()
                controller.press(keyboard.Key.caps_lock)
                controller.release(keyboard.Key.caps_lock)

        self._pool.submit(do_restore)

    def is_restoring(self, key: str) -> bool:
        """检查是否正在恢复指定按键"""
        return key in self._restoring_keys

    def clear_restoring_flag(self, key: str) -> None:
        """清除恢复标志"""
        self._restoring_keys.discard(key)

    # ========== 防自捕获检查 ==========

    def _check_emulating(self, key_name: str, msg: int, is_mouse: bool = False) -> bool:
        """检查是否正在模拟按键"""
        if not self._emulator.is_emulating(key_name):
            return False

        # 松开时清除标志
        if is_mouse:
            if msg == WM_XBUTTONUP:
                self._emulator.clear_emulating_flag(key_name)
        else:
            if msg in (WM_KEYUP, WM_SYSKEYUP):
                self._emulator.clear_emulating_flag(key_name)

        return True  # 放行

    def _check_restoring(self, key_name: str, msg: int) -> bool:
        """检查是否正在恢复按键"""
        if not self.is_restoring(key_name):
            return False

        if msg in (WM_KEYUP, WM_SYSKEYUP):
            self.clear_restoring_flag(key_name)

        return True  # 放行

    # ========== 钩子守护 ==========

    def _reinstall_hooks(self) -> bool:
        """
        重新安装键盘/鼠标钩子，把自己挪回钩子链的最前面。

        背景：低级键盘钩子（WH_KEYBOARD_LL）是全局链式调用的，**后装的钩子
        排在链前面**，前面的钩子可以把事件吞掉（返回非 0 表示"已处理"），
        链就断在那里，后面早先装上的钩子根本收不到按键。

        实测：某个以管理员权限运行的终端进程自己也装了低级钩子，它启动时间
        比客户端晚，装在链前面把按键吞了 —— 表现就是"在那个管理员窗口里按
        快捷键完全没反应，换到普通窗口又正常"，客户端日志里一条按键记录都没有。
        重启客户端后（钩子变成链里最新的）就恢复正常了。

        所以这里监听前台窗口变化：切到别的程序时重新装一次钩子，把自己顶回
        链首，问题就没了。
        """
        try:
            if self.keyboard_listener is not None:
                try:
                    self.keyboard_listener.stop()
                except Exception:
                    pass
                self.keyboard_listener = keyboard.Listener(
                    win32_event_filter=self.create_keyboard_filter()
                )
                self.keyboard_listener.start()

            if self.mouse_listener is not None:
                try:
                    self.mouse_listener.stop()
                except Exception:
                    pass
                self.mouse_listener = mouse.Listener(
                    win32_event_filter=self.create_mouse_filter()
                )
                self.mouse_listener.start()

            logger.debug('已重新安装钩子，回到钩子链最前面')
            return True
        except Exception as e:
            logger.warning(f'重新安装钩子失败: {e}')
            return False

    def _watch_foreground(self) -> None:
        """监听前台窗口变化，变化后重新安装钩子"""
        last_seen = 0
        last_reinstall = 0.0
        pending = False
        stale_since = 0.0

        while not self._hook_watch_stop.is_set():
            try:
                now = time.time()
                hwnd = _user32.GetForegroundWindow()
                # 取根窗口：子窗口、提示气泡之类的变化不算切换应用
                root = (_user32.GetAncestor(hwnd, 2) or hwnd) if hwnd else 0

                if root != last_seen:
                    last_seen = root
                    pending = True

                if self._pressed_keys:
                    # 按键还按着的时候不能换钩子：换掉之后收不到 keyup，
                    # 组合键状态会卡死（_combo_pressed 一直是 True）。
                    if not stale_since:
                        stale_since = now
                    elif now - stale_since > _STALE_KEY_STATE_TIMEOUT:
                        # 按键状态明显残留（多半是换钩子时漏掉了 keyup），清掉重来
                        logger.debug('按键状态残留，清空后重新安装钩子')
                        self._pressed_keys.clear()
                        self._combo_pressed.clear()
                        stale_since = 0.0
                        pending = True
                else:
                    stale_since = 0.0

                if pending and not self._pressed_keys and now - last_reinstall >= _REINSTALL_COOLDOWN:
                    if self._reinstall_hooks():
                        pending = False
                        last_reinstall = time.time()
            except Exception as e:
                logger.debug(f'钩子守护线程异常: {e}')

            self._hook_watch_stop.wait(_FOREGROUND_POLL_INTERVAL)

    # ========== 公共接口 ==========

    def start(self) -> None:
        """启动所有监听器"""
        has_keyboard = any(s.type == 'keyboard' for s in self.shortcuts if s.enabled)
        has_mouse = any(s.type == 'mouse' for s in self.shortcuts if s.enabled)

        if has_keyboard:
            if self.keyboard_listener and self.keyboard_listener.is_alive():
                logger.debug("键盘监听器已在运行，跳过启动")
            else:
                self.keyboard_listener = keyboard.Listener(
                    win32_event_filter=self.create_keyboard_filter()
                )
                self.keyboard_listener.start()
                logger.info("键盘监听器已启动")

        if has_mouse:
            if self.mouse_listener and self.mouse_listener.is_alive():
                logger.debug("鼠标监听器已在运行，跳过启动")
            else:
                self.mouse_listener = mouse.Listener(
                    win32_event_filter=self.create_mouse_filter()
                )
                self.mouse_listener.start()
                logger.info("鼠标监听器已启动")

        # 打印所有启用的快捷键
        for shortcut in self.shortcuts:
            if shortcut.enabled:
                mode = "长按" if shortcut.hold_mode else "单击"
                toggle = "可恢复" if shortcut.is_toggle_key() else "普通键"
                logger.info(f"  [{shortcut.key}] {mode}模式, 阻塞:{shortcut.suppress}, {toggle}")

        # 启动钩子守护线程
        self._hook_watch_stop.clear()
        if self._hook_watch_thread is None or not self._hook_watch_thread.is_alive():
            self._hook_watch_thread = threading.Thread(
                target=self._watch_foreground, name='HookWatch', daemon=True
            )
            self._hook_watch_thread.start()
            logger.info('钩子守护已启用（前台窗口切换时自动重装钩子）')

    def stop(self) -> None:
        """停止所有监听器和清理资源"""
        # 停掉钩子守护线程
        self._hook_watch_stop.set()
        if self._hook_watch_thread is not None:
            try:
                self._hook_watch_thread.join(timeout=1.0)
            except Exception:
                pass
            self._hook_watch_thread = None

        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
                logger.debug("键盘监听器已停止")
            except Exception:
                pass
            finally:
                self.keyboard_listener = None
                
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
                logger.debug("鼠标监听器已停止")
            except Exception:
                pass
            finally:
                self.mouse_listener = None

        # 取消所有任务
        for task in self.tasks.values():
            if task.is_recording:
                task.cancel()

        # 关闭线程池
        self._pool.shutdown(wait=False)
        logger.debug("快捷键管理器线程池已关闭")
