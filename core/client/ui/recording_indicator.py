# coding: utf-8
"""
录音状态悬浮指示器

只要正在录音，就在屏幕底部正中央显示一个置顶、鼠标穿透的圆角悬浮条；
停止录音后立即隐藏。用于快速确认当前是否正在录音。

实现方式：纯 Win32 API（ctypes），不依赖 tkinter / PyQt。
窗口运行在独立守护线程中，拥有自己的消息循环，
不会与已有的 ToastMessageThread、pynput 监听线程、音频回调线程争抢。

窗口特性：
    - 始终置顶，且不抢焦点（WS_EX_NOACTIVATE + SW_SHOWNOACTIVATE）
    - 鼠标穿透，点击直接落到下层程序（WS_EX_TRANSPARENT）
    - 不在任务栏显示（WS_EX_TOOLBAR）
    - 32 位带 alpha 通道的图层窗口（UpdateLayeredWindow），圆角和文字边缘平滑
    - 录音时红点带脉冲动画
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Optional

from .. import logger

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ============================================================
# Win32 常量
# ============================================================

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_ERASEBKGND = 0x0014
WM_TIMER = 0x0113
WM_APP_STATE = 0x8001
WM_APP_EXIT = 0x8002

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

WS_POPUP = 0x80000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLBAR = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

ULW_ALPHA = 0x00000002
DIB_RGB_COLORS = 0
AC_SRC_ALPHA = 0x01

SPI_GETWORKAREA = 0x0030

IDC_ARROW = 32512

_TIMER_ID = 0x01
_TIMER_MS = 50  # 录音时脉冲动画的刷新间隔（毫秒）

_MARGIN_BOTTOM = 56   # 距任务栏上沿的距离（逻辑像素）
_FONT_CANDIDATES = (
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\msyhl.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\segoeui.ttf',
    r'C:\Windows\Fonts\arial.ttf',
)

TEXT_RECORDING = 'Recording'


# ============================================================
# ctypes 结构体与函数签名
# ============================================================

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HANDLE),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ('bmiHeader', BITMAPINFOHEADER),
        ('bmiColors', wintypes.DWORD * 3),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ('BlendOp', wintypes.BYTE),
        ('BlendFlags', wintypes.BYTE),
        ('SourceConstantAlpha', wintypes.BYTE),
        ('AlphaFormat', wintypes.BYTE),
    ]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [
    wintypes.HWND, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p
]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
    ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT
]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
user32.ValidateRect.restype = wintypes.BOOL
user32.ValidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def _dpi_scale() -> float:
    """当前进程的 DPI 缩放比例，96 DPI 时为 1.0"""
    try:
        dpi = user32.GetDpiForSystem()
        if dpi and dpi > 0:
            return dpi / 96.0
    except (AttributeError, OSError):
        pass
    return 1.0


# ============================================================
# 渲染
# ============================================================

_pil_cache: dict = {}


def _get_pil():
    """按需导入 Pillow，避免缺包时模块导入即崩溃"""
    if 'pil' not in _pil_cache:
        try:
            from PIL import Image, ImageDraw, ImageFont
            _pil_cache['pil'] = (Image, ImageDraw, ImageFont)
        except ImportError as e:
            _pil_cache['pil'] = None
            logger.warning(f'录音指示器不可用，缺少 Pillow: {e}')
    return _pil_cache['pil']


def _get_font(font_module, size: int):
    """挑选一个系统中文字体，全部缺失时退回 Pillow 默认字体"""
    key = ('font', size)
    if key not in _pil_cache:
        font = None
        for path in _FONT_CANDIDATES:
            try:
                font = font_module.truetype(path, size)
                break
            except OSError:
                continue
        if font is None:
            try:
                font = font_module.load_default(size)
            except (OSError, TypeError):
                font = font_module.load_default()
        _pil_cache[key] = font
    return _pil_cache[key]


def _render_bgra(scale: float, phase: float) -> tuple:
    """
    绘制"录音中"悬浮条

    Args:
        scale: DPI 缩放比例
        phase: 脉冲动画相位 0.0 ~ 1.0

    Returns:
        (宽, 高, 预乘 alpha 的 BGRA 像素数据)
    """
    image_mod, draw_mod, font_mod = _get_pil()

    font_size = max(10, round(15 * scale))
    font = _get_font(font_mod, font_size)
    text_w = int(font.getlength(TEXT_RECORDING))

    dot_r = max(4, round(5.5 * scale))
    gap = max(5, round(9 * scale))
    pad_x = max(8, round(13 * scale))
    h = max(24, round(34 * scale))
    w = pad_x + dot_r * 2 + gap + text_w + pad_x

    img = image_mod.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = draw_mod.Draw(img)

    # 半透明深色圆角底板
    draw.rounded_rectangle(
        [0, 0, w - 1, h - 1],
        radius=h / 2,
        fill=(18, 18, 22, 205),
        outline=(255, 255, 255, 38),
        width=max(1, round(scale)),
    )

    # 向外扩散的脉冲光环
    cy = h / 2
    dot_cx = pad_x + dot_r
    halo_r = dot_r + round(6 * scale)
    halo_a = int(90 * (1.0 - phase))
    if halo_a > 0:
        draw.ellipse(
            [dot_cx - halo_r, cy - halo_r, dot_cx + halo_r, cy + halo_r],
            fill=(235, 64, 52, halo_a),
        )

    # 实心红点
    draw.ellipse(
        [dot_cx - dot_r, cy - dot_r, dot_cx + dot_r, cy + dot_r],
        fill=(235, 64, 52, 255),
    )

    draw.text(
        (dot_cx + dot_r + gap, cy),
        TEXT_RECORDING,
        font=font,
        fill=(255, 255, 255, 255),
        anchor='lm',
    )

    # RGBA -> 预乘 alpha 的 RGBa，再把 R/B 交换成 DIB 需要的 BGRA
    premul = img.convert('RGBa').tobytes()
    bgra = bytearray(premul)
    bgra[0::4], bgra[2::4] = premul[2::4], premul[0::4]

    return w, h, bytes(bgra)


# ============================================================
# 窗口过程
# ============================================================

_indicator = None  # 当前活动实例，供 WndProc 回调


def _wnd_proc_impl(hwnd, msg, wparam, lparam):
    ind = _indicator
    if ind is None:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    if msg == WM_APP_STATE:
        ind._refresh()
        return 0

    if msg == WM_APP_EXIT:
        user32.DestroyWindow(hwnd)
        return 0

    if msg == WM_TIMER:
        if wparam == _TIMER_ID:
            ind._paint()
        return 0

    if msg == WM_PAINT:
        # Layered windows submit their surface via UpdateLayeredWindow only.
        # Validate the region so the system stops resending WM_PAINT.
        user32.ValidateRect(hwnd, None)
        return 0

    if msg == WM_ERASEBKGND:
        return 1  # 图层窗口无需擦除背景，避免闪烁

    if msg == WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0

    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0

    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# 必须保持模块级引用，防止被垃圾回收后 Win32 回调野指针
_wnd_proc = WNDPROC(_wnd_proc_impl)


# ============================================================
# 指示器
# ============================================================

class RecordingIndicator:
    """
    录音状态悬浮指示器（仅录音时显示）

    典型用法::

        indicator = RecordingIndicator()
        indicator.start()
        indicator.set_recording(True)   # 显示
        indicator.set_recording(False)  # 隐藏
        indicator.stop()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recording = False
        self._hwnd = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._ok = False

        # 仅在窗口线程内使用的资源
        self._hdib = None
        self._old_obj = None
        self._mem_dc = None
        self._pv_bits = None
        self._canvas_size = (0, 0)
        self._bmi = BITMAPINFO()
        self._class_name = 'CapsWriterRecordingIndicator'

    # ---------- 状态 ----------

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    def set_recording(self, recording: bool) -> None:
        """更新录音状态。线程安全，可在任意线程调用。"""
        with self._lock:
            if self._recording == recording:
                return
            self._recording = recording
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_APP_STATE, 0, 0)

    # ---------- 生命周期 ----------

    def start(self, timeout: float = 3.0) -> bool:
        """启动指示器线程。返回是否成功。"""
        global _indicator
        if self._thread is not None:
            return True
        if _get_pil() is None:
            return False

        self._ready.clear()
        _indicator = self
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='RecordingIndicatorThread'
        )
        self._thread.start()

        if not self._ready.wait(timeout):
            logger.warning('录音指示器启动超时')
            return False
        if not self._ok:
            logger.warning('录音指示器窗口创建失败，功能已禁用')
            return False

        logger.info('录音状态指示器已启用（录音时显示于屏幕底部居中）')
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """停止指示器并释放窗口资源。"""
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_APP_EXIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._hwnd = None
        self._ready.clear()

    # ---------- 窗口线程 ----------

    def _run(self) -> None:
        try:
            if not self._register_class():
                self._ok = False
                self._ready.set()
                return
            if not self._create_window():
                self._ok = False
                self._ready.set()
                return
        except Exception as e:
            logger.warning(f'录音指示器初始化异常: {e}', exc_info=True)
            self._ok = False
            self._ready.set()
            return

        self._ok = True
        self._ready.set()

        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._cleanup()

    def _register_class(self) -> bool:
        try:
            wndclass = WNDCLASSW()
            wndclass.style = CS_HREDRAW | CS_VREDRAW
            wndclass.lpfnWndProc = _wnd_proc
            wndclass.hInstance = kernel32.GetModuleHandleW(None)
            wndclass.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(IDC_ARROW))
            wndclass.lpszClassName = self._class_name
            user32.RegisterClassW(ctypes.byref(wndclass))
            return True
        except Exception as e:
            logger.warning(f'录音指示器注册窗口类失败: {e}')
            return False

    def _create_window(self) -> bool:
        scale = _dpi_scale()
        w, h, _ = _render_bgra(scale, 0.0)

        if not self._alloc_dib(w, h):
            return False

        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLBAR
            | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            self._class_name,
            self._class_name,
            WS_POPUP,
            0, 0, w, h,
            None, None, kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            logger.warning('录音指示器创建窗口失败')
            return False

        self._hwnd = hwnd
        # 初始隐藏：只有录音时才显示
        user32.ShowWindow(hwnd, SW_HIDE)
        self._push_to_screen()
        return True

    def _alloc_dib(self, w: int, h: int) -> bool:
        """申请绘图位图与内存 DC"""
        self._free_dib()

        screen_dc = user32.GetDC(None)
        try:
            bmi = self._bmi
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0      # BI_RGB

            pv_bits = ctypes.c_void_p()
            hdib = gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                ctypes.byref(pv_bits), None, 0,
            )
            if not hdib:
                logger.warning('录音指示器创建 DIB 失败')
                return False

            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                gdi32.DeleteObject(hdib)
                logger.warning('录音指示器创建内存 DC 失败')
                return False

            self._hdib = hdib
            self._mem_dc = mem_dc
            self._old_obj = gdi32.SelectObject(mem_dc, hdib)
            self._pv_bits = pv_bits
            self._canvas_size = (w, h)
            return True
        finally:
            user32.ReleaseDC(None, screen_dc)

    def _free_dib(self) -> None:
        if self._mem_dc and self._old_obj:
            gdi32.SelectObject(self._mem_dc, self._old_obj)
        if self._mem_dc:
            gdi32.DeleteDC(self._mem_dc)
        if self._hdib:
            gdi32.DeleteObject(self._hdib)
        self._mem_dc = None
        self._hdib = None
        self._old_obj = None
        self._pv_bits = None

    def _cleanup(self) -> None:
        self._free_dib()
        self._hwnd = None

    # ---------- 绘制 ----------

    def _refresh(self) -> None:
        """录音状态变化：显示 + 启动动画，或隐藏"""
        if self.recording:
            # 先提交画面（此时窗口仍隐藏，UpdateLayeredWindow 才能正确生效），
            # 再显示，最后启动脉冲动画。
            self._paint()
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            user32.SetTimer(self._hwnd, _TIMER_ID, _TIMER_MS, None)
        else:
            user32.KillTimer(self._hwnd, _TIMER_ID)
            user32.ShowWindow(self._hwnd, SW_HIDE)

    def _paint(self) -> None:
        """渲染一帧并提交到屏幕"""
        hwnd = self._hwnd
        if not hwnd or not self._mem_dc:
            return

        try:
            scale = _dpi_scale()
            phase = (time.time() * 1.6) % 1.0
            w, h, pixels = _render_bgra(scale, phase)

            if not self._pv_bits or (w, h) != self._canvas_size:
                if not self._alloc_dib(w, h):
                    return

            if self._pv_bits:
                ctypes.memmove(self._pv_bits, pixels, len(pixels))

            self._push_to_screen()
        except Exception as e:
            logger.debug(f'录音指示器绘制异常: {e}')

    def _push_to_screen(self) -> None:
        """把内存 DC 的内容作为图层窗口提交到屏幕底部正中"""
        hwnd = self._hwnd
        if not hwnd or not self._mem_dc:
            return

        w, h = self._canvas_size

        work = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)

        x = (work.left + work.right - w) // 2
        y = work.bottom - h - int(round(_MARGIN_BOTTOM * _dpi_scale()))

        dst = wintypes.POINT(x, y)
        size = wintypes.SIZE(w, h)
        src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)

        screen_dc = user32.GetDC(None)
        try:
            if not user32.UpdateLayeredWindow(
                hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                self._mem_dc, ctypes.byref(src), 0,
                ctypes.byref(blend), ULW_ALPHA,
            ):
                logger.debug('UpdateLayeredWindow 调用失败')
        finally:
            user32.ReleaseDC(None, screen_dc)


# ============================================================
# 模块级便捷入口
# ============================================================

_indicator_lock = threading.Lock()


def get_indicator() -> Optional[RecordingIndicator]:
    """获取全局单例；未启动或启动失败时返回 None"""
    global _indicator
    with _indicator_lock:
        return _indicator


def _ensure_started() -> Optional[RecordingIndicator]:
    global _indicator
    with _indicator_lock:
        if _indicator is None:
            _indicator = RecordingIndicator()
        ind = _indicator
    if ind._thread is None and not ind.start():
        return None
    return ind


def show_recording_indicator() -> None:
    """开始录音时调用（两种快捷键、鼠标键、UDP 控制都会经过这里）"""
    try:
        ind = _ensure_started()
        if ind is not None:
            ind.set_recording(True)
    except Exception as e:
        logger.debug(f'显示录音指示器失败: {e}')


def hide_recording_indicator() -> None:
    """结束录音时调用"""
    try:
        ind = get_indicator()
        if ind is not None:
            ind.set_recording(False)
    except Exception as e:
        logger.debug(f'隐藏录音指示器失败: {e}')


def stop_recording_indicator() -> None:
    """客户端退出时调用"""
    global _indicator
    with _indicator_lock:
        if _indicator is not None:
            _indicator.stop()
            _indicator = None
