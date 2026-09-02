"""Windows desktop capture and dependency-free PNG encoding."""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Screenshot:
    width: int
    height: int
    png: bytes


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def encode_bgra_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode top-down BGRA pixels as an RGB PNG using only the standard library."""
    if width <= 0 or height <= 0:
        raise ValueError("screenshot dimensions must be positive")
    expected_size = width * height * 4
    if len(pixels) != expected_size:
        raise ValueError(f"invalid BGRA buffer size: {len(pixels)} (expected {expected_size})")

    source = memoryview(pixels)
    compressor = zlib.compressobj(level=6)
    compressed = bytearray()
    source_stride = width * 4
    for row_index in range(height):
        row_start = row_index * source_stride
        row = source[row_start : row_start + source_stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4].tobytes()
        rgb[1::3] = row[1::4].tobytes()
        rgb[2::3] = row[0::4].tobytes()
        compressed.extend(compressor.compress(b"\x00" + rgb))
    compressed.extend(compressor.flush())

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", bytes(compressed))
        + _png_chunk(b"IEND", b"")
    )


def _win32_error(action: str, ctypes_module: Any) -> OSError:
    error = ctypes_module.get_last_error()
    return OSError(error, f"{action} failed (Win32 error {error})")


def capture_screenshot() -> Screenshot:
    """Capture the complete Windows virtual desktop as a PNG image."""
    if os.name != "nt":
        raise RuntimeError("screenshots are only supported by a Windows Agent")

    import ctypes
    from ctypes import wintypes

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", wintypes.BYTE),
            ("rgbGreen", wintypes.BYTE),
            ("rgbRed", wintypes.BYTE),
            ("rgbReserved", wintypes.BYTE),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    # Ask Windows for physical pixels on high-DPI and mixed-DPI desktops.
    set_dpi_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous_dpi_context = None
    if set_dpi_context is not None:
        set_dpi_context.argtypes = [ctypes.c_void_p]
        set_dpi_context.restype = ctypes.c_void_p
        previous_dpi_context = set_dpi_context(ctypes.c_void_p(-4))

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    SRCCOPY = 0x00CC0020
    CAPTUREBLT = 0x40000000
    DIB_RGB_COLORS = 0
    BI_RGB = 0

    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        raise RuntimeError("Windows did not report a usable desktop")

    screen_dc = None
    memory_dc = None
    bitmap = None
    previous_object = None
    bitmap_selected = False
    try:
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise _win32_error("GetDC", ctypes)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise _win32_error("CreateCompatibleDC", ctypes)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            raise _win32_error("CreateCompatibleBitmap", ctypes)
        previous_object = gdi32.SelectObject(memory_dc, bitmap)
        if not previous_object or previous_object == ctypes.c_void_p(-1).value:
            raise _win32_error("SelectObject", ctypes)
        bitmap_selected = True
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            left,
            top,
            SRCCOPY | CAPTUREBLT,
        ):
            raise _win32_error("BitBlt", ctypes)

        # GetDIBits requires the bitmap not to be selected into a device context.
        gdi32.SelectObject(memory_dc, previous_object)
        bitmap_selected = False
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # Negative requests top-down scanlines.
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        byte_count = width * height * 4
        buffer = ctypes.create_string_buffer(byte_count)
        rows = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if rows != height:
            raise _win32_error("GetDIBits", ctypes)
        return Screenshot(width=width, height=height, png=encode_bgra_png(width, height, buffer.raw))
    finally:
        if bitmap_selected and memory_dc and previous_object:
            gdi32.SelectObject(memory_dc, previous_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        if set_dpi_context is not None and previous_dpi_context:
            set_dpi_context(previous_dpi_context)
