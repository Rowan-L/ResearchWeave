"""PyInstaller runtime hook(F23 精简版):显式 AppUserModelID + 自动建桌面快捷方式。

背景:Windows 11 任务栏对未设置 AppUserModelID 的 PyInstaller/Qt 窗口,按钮
图标会退回系统默认——实测窗口 WM_SETICON 图标正常、exe 内嵌图标正常、
SHDefExtractIcon 提取正常,任务栏仍不采用(改路径/重启 explorer 亦然)。
修复 = ①进程在任何窗口创建前设置显式 AUMID;②存在**带同名 AUMID 属性**的
快捷方式(任务栏按 AUMID 匹配快捷方式取图标)。两者缺一不可,已实测。

本 hook 首次运行时在桌面自动创建 "ResearchWeaveLite.lnk"(指向当前 exe,
带 AUMID 属性),之后任务栏/开始菜单图标即正常;已存在则跳过。全程失败
静默——图标展示属锦上添花,绝不阻断启动。仅存在于精简版构建,共享源码
零改动;AUMID 只影响任务栏分组/图标,与单实例锁、数据目录无关。
"""

import ctypes
import os
import sys

AUMID = "SynthRecall.ResearchWeave"


def _set_aumid() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:  # noqa: BLE001, S110 - 失败不阻断启动
        pass


def _desktop_dir() -> str:
    # SHGetSpecialFolderPathW,CSIDL_DESKTOPDIRECTORY = 0x10(考虑重定向)
    buf = ctypes.create_unicode_buffer(520)
    if ctypes.windll.shell32.SHGetSpecialFolderPathW(0, buf, 0x10, False):
        return buf.value
    return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def _ensure_shortcut() -> None:
    """首次运行在桌面创建带 AUMID 属性的快捷方式(幂等,失败静默)。"""
    try:
        exe_path = os.path.abspath(sys.executable)  # 冻结模式 = 当前 exe
        lnk_path = os.path.join(_desktop_dir(), "ResearchWeaveLite.lnk")
        if os.path.exists(lnk_path):
            return

        ole32 = ctypes.oledll.ole32

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
            ]

        def guid(s):
            g = _GUID()
            parts = s.strip("{}").split("-")
            g.Data1 = int(parts[0], 16)
            g.Data2 = int(parts[1], 16)
            g.Data3 = int(parts[2], 16)
            raw = parts[3] + parts[4]
            for i in range(8):
                g.Data4[i] = int(raw[i * 2 : i * 2 + 2], 16)
            return g

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]

        def propkey(s, pid):
            pk = PROPERTYKEY()
            ctypes.memmove(ctypes.byref(pk), ctypes.byref(guid(s)), 16)
            pk.pid = pid
            return pk

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("pwszVal", ctypes.c_wchar_p),
            ]

        CLSCTX_INPROC_SERVER = 1
        VT_LPWSTR = 31
        clsid = guid("00021401-0000-0000-C000-000000000046")
        iid_sl = guid("000214F9-0000-0000-C000-000000000046")
        iid_pf = guid("0000010B-0000-0000-C000-000000000046")
        iid_ps = guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
        pkey = propkey("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", 5)

        ole32.CoInitializeEx(None, 2)
        psl = ctypes.c_void_p()
        if ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(iid_sl), ctypes.byref(psl),
        ) != 0:
            return
        vtbl = ctypes.cast(
            ctypes.cast(psl, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        def method(ptr, index, *args):
            m = {str: ctypes.c_wchar_p, int: ctypes.c_int}
            ftype = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, *[m[type(a)] for a in args]
            )
            return ctypes.cast(vtbl[index], ftype)(ptr, *args)

        def pass_ok(hr):
            return hr in (0, 1)  # S_OK / S_FALSE

        # IShellLinkW: 3=GetPath,4=GetIDList,5=SetIDList,8=GetWorkingDirectory,
        # 9=SetWorkingDirectory,16=GetIconLocation,17=SetIconLocation,21=SetPath
        if not pass_ok(method(psl, 9, os.path.dirname(exe_path))):
            return
        if not pass_ok(method(psl, 17, exe_path, 0)):
            return
        shell32 = ctypes.windll.shell32
        pidl = ctypes.c_void_p()
        shell32.SHParseDisplayName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ]
        shell32.SHParseDisplayName.restype = ctypes.HRESULT
        if shell32.SHParseDisplayName(
            exe_path, None, ctypes.byref(pidl), 0, None
        ) != 0:
            return
        set_idlist = ctypes.cast(vtbl[5], ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p
        ))
        if not pass_ok(set_idlist(psl, pidl)):
            return
        ole32.CoTaskMemFree(pidl)

        pps = ctypes.c_void_p()
        qi = ctypes.cast(vtbl[0], ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ))
        if not pass_ok(qi(psl, ctypes.byref(iid_ps), ctypes.byref(pps))):
            return
        store_vtbl = ctypes.cast(
            ctypes.cast(pps, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        pv = PROPVARIANT()
        pv.vt = VT_LPWSTR
        pv.pwszVal = AUMID
        set_value = ctypes.cast(store_vtbl[6], ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(PROPVARIANT),
        ))
        if not pass_ok(set_value(pps, ctypes.byref(pkey), ctypes.byref(pv))):
            return
        if not pass_ok(ctypes.cast(store_vtbl[7], ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p
        ))(pps)):
            return

        ppf = ctypes.c_void_p()
        if not pass_ok(qi(psl, ctypes.byref(iid_pf), ctypes.byref(ppf))):
            return
        pf_vtbl = ctypes.cast(
            ctypes.cast(ppf, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        # IPersistFile 继承 IPersist:GetClassID 占 3,4=IsDirty,5=Load,6=Save
        save = ctypes.cast(pf_vtbl[6], ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
        ))
        save(ppf, lnk_path, True)
    except Exception:  # noqa: BLE001, S110 - 快捷方式创建失败不阻断启动
        pass


if sys.platform == "win32":
    _set_aumid()
    try:
        _ensure_shortcut()
    except Exception:  # noqa: BLE001, S110 - 快捷方式创建失败不阻断启动
        pass
