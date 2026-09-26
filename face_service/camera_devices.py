"""Video capture devices by NAME (Stage 9, act 9b R10 / F-141, F-150).

OpenCV opens cameras by index only, and DirectShow and Media Foundation number their devices
independently -- "index 0" could be a different camera (or a virtual one) per backend, and indices
shift on hotplug. So the product stores the camera's friendly NAME (config ``camera_name``) and
resolves it to a DirectShow index at every open, by enumerating the DirectShow video-input
category -- the same category, in the same order, that OpenCV's CAP_DSHOW backend enumerates.

The enumeration is plain COM over ctypes (ICreateDevEnum -> IEnumMoniker -> IMoniker ->
IPropertyBag): no new dependency, nothing to sign or license. It reads device properties only; it
never opens the camera, so it is safe while another process (or the service) holds the device.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_ulong, c_void_p, wintypes
from typing import NamedTuple

log = logging.getLogger(__name__)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s: str) -> "GUID":
        g = cls()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(g))
        return g


CLSID_SystemDeviceEnum = "{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}"
IID_ICreateDevEnum = "{29840822-5B84-11D0-BD3B-00A0C911CE86}"
CLSID_VideoInputDeviceCategory = "{860BB310-5D01-11d0-BD3B-00A0C911CE86}"
IID_IPropertyBag = "{55272A00-42CB-11CE-8135-00AA004BB851}"

CLSCTX_INPROC_SERVER = 1
COINIT_MULTITHREADED = 0
RPC_E_CHANGED_MODE = -2147417850 & 0xFFFFFFFF
VT_BSTR = 8


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                ("r3", wintypes.WORD), ("val", c_void_p), ("pad", c_void_p)]


class CameraDevice(NamedTuple):
    index: int          # position in the DirectShow enumeration == the CAP_DSHOW index
    name: str           # FriendlyName, as the user sees it
    path: str           # DevicePath (symbolic link) -- "" for some virtual cameras


def _vcall(obj: c_void_p, index: int, restype, *args_types):
    """A function pointer to method ``index`` of a COM object's vtable."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, c_void_p, *args_types)(vtbl[index])


def _release(obj) -> None:
    if obj:
        try:
            _vcall(obj, 2, c_ulong)(obj)
        except Exception:
            pass


def _read_prop(bag: c_void_p, name: str) -> str:
    var = VARIANT()
    hr = _vcall(bag, 3, ctypes.HRESULT, ctypes.c_wchar_p, POINTER(VARIANT), c_void_p)
    try:
        hr(bag, name, byref(var), None)
    except OSError:
        return ""
    try:
        if var.vt == VT_BSTR and var.val:
            return ctypes.wstring_at(var.val)
        return ""
    finally:
        ctypes.oledll.oleaut32.VariantClear(byref(var))


def list_video_devices() -> "list[CameraDevice]":
    """The DirectShow video-input devices in CAP_DSHOW order. [] when there are none or when
    enumeration is impossible (logged). Never raises."""
    ole32 = ctypes.oledll.ole32
    inited = False
    try:
        try:
            ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            inited = True
        except OSError as e:
            if (e.winerror or 0) & 0xFFFFFFFF != RPC_E_CHANGED_MODE:
                raise
        return _enumerate()
    except Exception as e:
        log.warning("camera enumeration failed: %r", e)
        return []
    finally:
        if inited:
            ole32.CoUninitialize()


def _enumerate() -> "list[CameraDevice]":
    ole32 = ctypes.oledll.ole32
    devenum = c_void_p()
    ole32.CoCreateInstance(byref(GUID.parse(CLSID_SystemDeviceEnum)), None, CLSCTX_INPROC_SERVER,
                           byref(GUID.parse(IID_ICreateDevEnum)), byref(devenum))
    out: list = []
    enum = c_void_p()
    try:
        create = _vcall(devenum, 3, ctypes.c_long, POINTER(GUID), POINTER(c_void_p), wintypes.DWORD)
        hr = create(devenum, byref(GUID.parse(CLSID_VideoInputDeviceCategory)), byref(enum), 0)
        if hr != 0 or not enum:        # S_FALSE: the category is empty
            return []
        nxt = _vcall(enum, 3, ctypes.c_long, c_ulong, POINTER(c_void_p), POINTER(c_ulong))
        bind = None
        index = 0
        while True:
            mon = c_void_p()
            fetched = c_ulong(0)
            if nxt(enum, 1, byref(mon), byref(fetched)) != 0 or not fetched.value:
                break
            try:
                if bind is None:
                    # IMoniker::BindToStorage is slot 9: IUnknown(3) + IPersist(1) + IPersistStream(4)
                    # + BindToObject(8) -> BindToStorage(9).
                    bind = 9
                bag = c_void_p()
                f = _vcall(mon, bind, ctypes.c_long, c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p))
                if f(mon, None, None, byref(GUID.parse(IID_IPropertyBag)), byref(bag)) == 0 and bag:
                    try:
                        out.append(CameraDevice(index, _read_prop(bag, "FriendlyName"),
                                                _read_prop(bag, "DevicePath")))
                    finally:
                        _release(bag)
                index += 1
            finally:
                _release(mon)
    finally:
        _release(enum)
        _release(devenum)
    return out


def resolve_index(name: str, devices: "list[CameraDevice] | None" = None) -> "int | None":
    """The CAP_DSHOW index of the camera called ``name`` right now, or None when it is not
    present. An exact name match wins; a unique case-insensitive match is accepted too. With
    two cameras of the same name the first is taken (the enumeration order is the driver's)."""
    devices = list_video_devices() if devices is None else devices
    want = (name or "").strip()
    if not want:
        return None
    exact = [d for d in devices if d.name == want]
    if exact:
        return exact[0].index
    loose = [d for d in devices if d.name.lower() == want.lower()]
    return loose[0].index if len(loose) == 1 else None
