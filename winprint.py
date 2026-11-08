"""USB 列印：透過 Windows 列印佇列把原始資料 (RAW) 送給印表機，不經過驅動程式轉換。

列印佇列只能單向送資料；查詢狀態時改為直接開啟 USB 印表機裝置（USBPRINT），送出指令並讀回應。
只用 ctypes 呼叫 Windows API，不需額外套件。所有錯誤都丟 OSError，與網路列印的錯誤處理一致。
"""
import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes

PRINTER_ENUM_LOCAL = 2
PRINTER_ENUM_CONNECTIONS = 4
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400
JOB_CONTROL_DELETE = 5
ERROR_INSUFFICIENT_BUFFER = 122

# GetPrinter level 6 的狀態旗標 → 中文說明（只列會讓列印失敗的）
STATUS_FLAGS = {
    0x00000001: "暫停中",
    0x00000002: "錯誤",
    0x00000010: "缺紙",
    0x00000080: "離線（USB 線沒接好或印表機沒開機）",
    0x00001000: "無法使用",
    0x00100000: "需要人員處理",
    0x00400000: "上蓋沒有蓋好",
}


class DOC_INFO_1(ctypes.Structure):
    _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]


class PRINTER_INFO_4(ctypes.Structure):
    _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD)]


class PRINTER_INFO_5(ctypes.Structure):
    _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pPortName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD),
                ("DeviceNotSelectedTimeout", wintypes.DWORD), ("TransmissionRetryTimeout", wintypes.DWORD)]


# USB 印表機裝置介面 GUID_DEVINTERFACE_USBPRINT；登錄檔裡記錄每個裝置對應的連接埠（USB001…）
USBPRINT_CLASS = r"SYSTEM\CurrentControlSet\Control\DeviceClasses\{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"


_ws = None


def _winspool():
    global _ws
    if os.name != "nt":
        raise OSError("USB 列印只支援 Windows")
    if _ws is None:
        ws = ctypes.WinDLL("winspool.drv", use_last_error=True)
        H, P, D, B = wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL
        for name, args, res in [
            ("OpenPrinterW", [wintypes.LPWSTR, ctypes.POINTER(H), P], B),
            ("ClosePrinter", [H], B),
            ("StartDocPrinterW", [H, D, ctypes.POINTER(DOC_INFO_1)], D),
            ("EndDocPrinter", [H], B),
            ("StartPagePrinter", [H], B),
            ("EndPagePrinter", [H], B),
            ("WritePrinter", [H, P, D, ctypes.POINTER(D)], B),
            ("GetPrinterW", [H, D, P, D, ctypes.POINTER(D)], B),
            ("GetJobW", [H, D, D, P, D, ctypes.POINTER(D)], B),
            ("SetJobW", [H, D, D, P, D], B),
            ("EnumPrintersW", [D, wintypes.LPWSTR, D, P, D, ctypes.POINTER(D), ctypes.POINTER(D)], B),
        ]:
            fn = getattr(ws, name)
            fn.argtypes, fn.restype = args, res
        _ws = ws
    return _ws


def _error(what):
    return OSError(f"{what}失敗（Windows 錯誤碼 {ctypes.get_last_error()}）")


def list_printers():
    """回傳 [(名稱, 是否離線)]；非 Windows 回傳空清單。"""
    if os.name != "nt":
        return []
    ws = _winspool()
    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed, count = wintypes.DWORD(), wintypes.DWORD()
    ws.EnumPrintersW(flags, None, 4, None, 0, ctypes.byref(needed), ctypes.byref(count))
    if not needed.value:
        return []
    buf = ctypes.create_string_buffer(needed.value)
    if not ws.EnumPrintersW(flags, None, 4, buf, needed, ctypes.byref(needed), ctypes.byref(count)):
        raise _error("列出印表機")
    infos = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_4))
    return [
        (infos[i].pPrinterName, bool(infos[i].Attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE))
        for i in range(count.value)
    ]


class _Printer:
    def __init__(self, name):
        self.ws = _winspool()
        self.h = wintypes.HANDLE()
        if not name or not self.ws.OpenPrinterW(name, ctypes.byref(self.h), None):
            raise OSError(f"找不到 Windows 印表機「{name}」，請到設定頁重新選擇")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.ws.ClosePrinter(self.h)

    def _info(self, level, min_size):
        # Windows 要求的緩衝區可能比結構大（字串放在後面），先問需要多少
        needed = wintypes.DWORD()
        self.ws.GetPrinterW(self.h, level, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(max(needed.value, min_size))
        if not self.ws.GetPrinterW(self.h, level, buf, len(buf), ctypes.byref(needed)):
            raise _error("查詢印表機狀態")
        return buf

    def status(self):
        return wintypes.DWORD.from_buffer(self._info(6, ctypes.sizeof(wintypes.DWORD))).value

    def port(self):
        buf = self._info(5, ctypes.sizeof(PRINTER_INFO_5))
        return ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_5))[0].pPortName or ""

    def job_exists(self, job_id):
        # 不給緩衝區：工作還在佇列 → 回傳「緩衝區不足」；已送完 → 回傳「參數錯誤」
        needed = wintypes.DWORD()
        self.ws.GetJobW(self.h, job_id, 1, None, 0, ctypes.byref(needed))
        return ctypes.get_last_error() == ERROR_INSUFFICIENT_BUFFER

    def cancel(self, job_id):
        self.ws.SetJobW(self.h, job_id, 0, None, JOB_CONTROL_DELETE)


def check(name):
    """確認印表機存在且 Windows 沒有回報離線或錯誤；有問題丟 OSError。"""
    offline = dict(list_printers()).get(name)
    with _Printer(name) as p:
        st = p.status()
    problems = [text for flag, text in STATUS_FLAGS.items() if st & flag]
    if offline:
        problems.append("Windows 設為「離線使用印表機」")
    if problems:
        raise OSError(f"Windows 回報印表機「{name}」{'、'.join(problems)}")


def _usb_device_path(port):
    """連接埠名稱（USB001）→ USB 裝置路徑；找不到（例如網路分享的印表機）回傳 None。"""
    import winreg
    try:
        cls = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, USBPRINT_CLASS)
    except OSError:
        return None
    with cls:
        for i in range(winreg.QueryInfoKey(cls)[0]):
            sub = winreg.EnumKey(cls, i)
            try:
                with winreg.OpenKey(cls, sub + r"\#\Device Parameters") as k:
                    base = winreg.QueryValueEx(k, "Base Name")[0]
                    num = winreg.QueryValueEx(k, "Port Number")[0]
            except OSError:
                continue
            if f"{base}{num:03d}".upper() == port.upper():
                return "\\\\?\\" + sub[4:]  # 子機碼名稱是 "##?#USB#VID_…"，對應路徑 "\\?\USB#VID_…"
    return None


_k32 = None


def _kernel32():
    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, P, D = wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, D, D, P, D, D, H]
        k.CreateFileW.restype = H
        k.WriteFile.argtypes = [H, P, D, ctypes.POINTER(D), P]
        k.ReadFile.argtypes = [H, P, D, ctypes.POINTER(D), P]
        k.CancelIoEx.argtypes = [H, P]
        k.CloseHandle.argtypes = [H]
        _k32 = k
    return _k32


def ask(name, request, first_timeout=2, idle_timeout=0.5, max_bytes=8192):
    """直接對 USB 裝置送出查詢並讀回應（雙向）。
    等第一個位元組最多 first_timeout 秒，之後閒置 idle_timeout 秒即結束；
    這台印表機不是 USB 直連（沒有 USB 裝置可讀）時回傳 b""。"""
    with _Printer(name) as p:
        path = _usb_device_path(p.port())
    if not path:
        return b""
    k = _kernel32()
    h = k.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)  # 讀寫、共用讀寫、OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        raise _error("開啟 USB 印表機")
    chunks, stop = queue.Queue(), threading.Event()

    def reader():
        buf, got = ctypes.create_string_buffer(4096), wintypes.DWORD()
        while not stop.is_set():
            if not k.ReadFile(h, buf, len(buf), ctypes.byref(got), None):
                return
            if got.value:
                chunks.put(buf.raw[:got.value])
            else:
                time.sleep(0.05)

    try:
        written = wintypes.DWORD()
        if not k.WriteFile(h, request, len(request), ctypes.byref(written), None):
            raise _error("送出查詢")
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        reply, timeout = b"", first_timeout
        while len(reply) < max_bytes:
            try:
                reply += chunks.get(timeout=timeout)
            except queue.Empty:
                break
            timeout = idle_timeout
        stop.set()
        k.CancelIoEx(h, None)  # 讓卡在 ReadFile 的執行緒結束
        t.join(1)
    finally:
        k.CloseHandle(h)
    return reply


def send_raw(name, data, doc_name="AutoLogo", timeout=30):
    """送出 RAW 資料並等它離開 Windows 列印佇列。

    超過 timeout 秒仍卡在佇列就取消該工作再丟 OSError，避免印表機恢復後才補印，
    造成「從失敗處繼續」時重複列印。"""
    check(name)
    with _Printer(name) as p:
        ws = p.ws
        doc = DOC_INFO_1(doc_name, None, "RAW")
        job_id = ws.StartDocPrinterW(p.h, 1, ctypes.byref(doc))
        if not job_id:
            raise _error("建立列印工作")
        try:
            ws.StartPagePrinter(p.h)
            written = wintypes.DWORD()
            ok = ws.WritePrinter(p.h, data, len(data), ctypes.byref(written))
            ws.EndPagePrinter(p.h)
        finally:
            ws.EndDocPrinter(p.h)
        if not ok or written.value != len(data):
            p.cancel(job_id)
            raise _error("寫入列印資料")
        deadline = time.monotonic() + timeout
        while p.job_exists(job_id):
            if time.monotonic() > deadline:
                p.cancel(job_id)
                raise OSError(f"資料在 Windows 列印佇列等了 {timeout} 秒仍未送到印表機，已取消這張。"
                              "請確認 USB 線與印表機電源後再試")
            time.sleep(0.2)
