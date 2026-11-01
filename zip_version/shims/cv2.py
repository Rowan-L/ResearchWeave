"""cv2 兼容层(zip 精简版专用,构建期注入,绝不影响完整版)。

背景(F23 极致瘦身):opencv-python-headless 在打包产物里占 112MB,而 vendored
BabelDOC 实际用到的 API 面不足 15 个函数。本文件用 numpy+PIL 等价实现这一子集,
配合 ResearchWeaveLite.spec 的 excludes=['cv2', ...] 与 runtime hook
(zip_version/pyi_rth_shims.py),在精简包里顶替真 cv2。

像素级关键路径(doclayout 预处理 resize/pad、扫描页检测 gray)按 OpenCV 的
整数定点公式逐位复刻;debug 画框/写字路径只求功能等价。未实现的 API 一律
raise NotImplementedError(fail loud),绝不静默给出错误值。
对拍依据见 zip_version/tests/test_shim_parity.py。
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFont

__version__ = "5.0.0-synthrecall-zip-shim"

# ---- 常量(数值与 OpenCV 一致;仅 BabelDOC 用到的那些) ----
INTER_NEAREST = 0
INTER_LINEAR = 1
INTER_CUBIC = 2
INTER_AREA = 3

BORDER_CONSTANT = 0
BORDER_REPLICATE = 1

COLOR_BGR2BGRA = 0
COLOR_RGB2BGR = 4
COLOR_BGR2RGB = 4
COLOR_BGR2GRAY = 6
COLOR_RGB2GRAY = 7

FONT_HERSHEY_SIMPLEX = 0
IMREAD_COLOR = 1
IMREAD_UNCHANGED = -1

# OpenCV 5.0 灰度转换定点系数(15 位,半上舍入),由对拍暴力搜索实测确定:
# y = (c0*a0 + c1*a1 + c2*a2 + (1<<14)) >> 15
# RGB2GRAY=(9798,19235,3735),BGR2GRAY 通道对调。逐位对拍见 test_shim_parity.py。
_GRAY_COEFF = {
    COLOR_RGB2GRAY: (9798, 19235, 3735),
    COLOR_BGR2GRAY: (3735, 19235, 9798),
}

_RESIZE_COEF_SCALE = 1 << 11  # OpenCV INTER_RESIZE_COEF_SCALE = 2048


def _fail(name: str, detail: str = "") -> NotImplementedError:
    msg = f"cv2 shim: {name} 未实现(精简版只覆盖 BabelDOC 用到的 API 面)"
    if detail:
        msg += f": {detail}"
    return NotImplementedError(msg)


# ---------------------------------------------------------------------------
# cvtColor
# ---------------------------------------------------------------------------

def cvtColor(src, code, dst=None, dstCn=None):
    arr = np.asarray(src)
    if code in (COLOR_RGB2BGR, COLOR_BGR2RGB):
        return np.ascontiguousarray(arr[..., ::-1])
    if code in _GRAY_COEFF:
        if arr.ndim != 3 or arr.shape[2] != 3:
            raise ValueError("cvtColor 灰度转换要求 HxWx3 输入")
        c0, c1, c2 = _GRAY_COEFF[code]
        acc = (
            arr[..., 0].astype(np.int32) * c0
            + arr[..., 1].astype(np.int32) * c1
            + arr[..., 2].astype(np.int32) * c2
            + (1 << 14)
        )
        return (acc >> 15).astype(np.uint8)
    raise _fail("cvtColor", f"code={code}")


# ---------------------------------------------------------------------------
# copyMakeBorder(仅 BORDER_CONSTANT)
# ---------------------------------------------------------------------------

def copyMakeBorder(src, top, bottom, left, right, borderType, value=None):
    if borderType != BORDER_CONSTANT:
        raise _fail("copyMakeBorder", f"borderType={borderType}")
    arr = np.asarray(src)
    if value is None:
        value = 0
    out_shape = (arr.shape[0] + top + bottom, arr.shape[1] + left + right)
    if arr.ndim == 3:
        out_shape = out_shape + (arr.shape[2],)
    out = np.zeros(out_shape, dtype=arr.dtype)
    if arr.ndim == 3 and np.isscalar(value):
        # OpenCV Scalar 语义:标量只填第 0 通道,其余为 0
        out[..., 0] = value
    else:
        out[...] = value  # 逐通道元组/灰度标量直接广播
    out[top : top + arr.shape[0], left : left + arr.shape[1]] = arr
    return out


# ---------------------------------------------------------------------------
# resize(仅 INTER_LINEAR;uint8 按 OpenCV 定点公式逐位复刻)
# ---------------------------------------------------------------------------

def _linear_tabs(src_size: int, dst_size: int):
    """复刻 OpenCV INTER_LINEAR 查表(对拍校准所得语义):

    - 坐标 float64 计算后立刻转 float32(与 ``float fx = (float)(...)`` 一致);
    - sx1 可为 -1 或 src_size-1(源行按 1 像素边缘复刻扩宽),系数不因此清零;
    - 系数 = cvRound(frac*2048)(float32 半偶舍入),c1 + c2 = 2048。
    """
    scale = src_size / dst_size
    xs = ((np.arange(dst_size, dtype=np.float64) + 0.5) * scale - 0.5).astype(np.float32)
    sx1 = np.floor(xs.astype(np.float64)).astype(np.int64)
    frac = xs - sx1.astype(np.float32)
    c2 = np.rint(frac * np.float32(_RESIZE_COEF_SCALE)).astype(np.int64)
    c1 = _RESIZE_COEF_SCALE - c2
    t1 = np.clip(sx1, 0, src_size - 1)
    t2 = np.clip(sx1 + 1, 0, src_size - 1)
    return t1, t2, c1, c2


def _resize_linear_u8(src: np.ndarray, dw: int, dh: int) -> np.ndarray:
    h, w = src.shape[:2]
    squeeze = src.ndim == 2
    img = src[:, :, None] if squeeze else src

    # 水平:11 位系数精确整数(与 cv2 5.x HResizeLinear 一致,对拍逐位相同)
    sx1, sx2, cx1, cx2 = _linear_tabs(w, dw)
    if dw != w:
        rows = cx1[None, :, None] * img[:, sx1, :] + cx2[None, :, None] * img[:, sx2, :]
    else:
        rows = img
    # 垂直:OpenCV 5.x VResizeLinear<uchar> 公式(标量与 SIMD 主路径同构):
    #   out = ((b0*(S0>>4))>>16 + (b1*(S1>>4))>>16 + 2) >> 2,逐级截断。
    # 对拍:99.98% 像素逐位一致,残差 0.02% 为 ±1 LSB(见 test_shim_parity.py
    # 的校准记录);doclayout ONNX 框级输出与真库一致。
    sy1, sy2, cy1, cy2 = _linear_tabs(h, dh)
    if dh != h:
        t0 = (cy1[:, None, None] * (rows[sy1, :, :] >> 4)) >> 16
        t1 = (cy2[:, None, None] * (rows[sy2, :, :] >> 4)) >> 16
        out = (t0 + t1 + 2) >> 2
    else:
        out = rows
    out = np.clip(out, 0, 255).astype(np.uint8)
    return out[:, :, 0] if squeeze else out


def _resize_linear_generic(src: np.ndarray, dw: int, dh: int) -> np.ndarray:
    """非 uint8 的通用双线性(精简包内 BabelDOC 不会走到,保底实现)。"""
    h, w = src.shape[:2]
    squeeze = src.ndim == 2
    img = (src[:, :, None] if squeeze else src).astype(np.float64)

    scale_x, scale_y = w / dw, h / dh
    fx = np.maximum((np.arange(dw) + 0.5) * scale_x - 0.5, 0.0)
    fy = np.maximum((np.arange(dh) + 0.5) * scale_y - 0.5, 0.0)
    x1 = np.floor(fx).astype(np.int64)
    y1 = np.floor(fy).astype(np.int64)
    x1 = np.minimum(x1, w - 1)
    y1 = np.minimum(y1, h - 1)
    x2 = np.minimum(x1 + 1, w - 1)
    y2 = np.minimum(y1 + 1, h - 1)
    dx = (fx - x1)[None, :, None]
    dy = (fy - y1)[:, None, None]

    top = img[:, x1, :] * (1 - dx) + img[:, x2, :] * dx
    bot = img[:, x1, :] * (1 - dx) + img[:, x2, :] * dx
    out = top[y1] * (1 - dy) + bot[y2] * dy
    out = out.astype(src.dtype)
    return out[:, :, 0] if squeeze else out


def resize(src, dsize, dst=None, fx=None, fy=None, interpolation=INTER_LINEAR):
    if interpolation != INTER_LINEAR:
        raise _fail("resize", f"interpolation={interpolation}")
    dw, dh = dsize[0], dsize[1]
    arr = np.ascontiguousarray(np.asarray(src))
    if arr.dtype == np.uint8:
        return _resize_linear_u8(arr, dw, dh)
    return _resize_linear_generic(arr, dw, dh)


# ---------------------------------------------------------------------------
# 文件读写 / 编码(PIL 等价;imwrite/imencode 用于 debug 与 RPC 路径)
# ---------------------------------------------------------------------------

def _to_pil(img):
    arr = np.asarray(img)
    if arr.ndim == 2:
        return Image.fromarray(arr)
    if arr.shape[2] == 3:
        return Image.fromarray(np.ascontiguousarray(arr[..., ::-1]))
    raise _fail("_to_pil", f"shape={arr.shape}")


def _from_pil(pil_img):
    rgb = np.asarray(pil_img.convert("RGB"))
    return np.ascontiguousarray(rgb[..., ::-1])


def imread(path, flags=IMREAD_COLOR):
    try:
        with Image.open(path) as im:
            return _from_pil(im)
    except Exception:  # noqa: BLE001 - 与 cv2.imread 行为一致:任何失败返回 None
        return None


def imwrite(path, img, params=None):
    _to_pil(img).save(path)
    return True


def imencode(ext, img, params=None):
    fmt = str(ext).lstrip(".").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    buf = io.BytesIO()
    _to_pil(img).save(buf, format=fmt, quality=95)
    data = buf.getvalue()
    return True, np.frombuffer(data, dtype=np.uint8)


# ---------------------------------------------------------------------------
# 画框 / 写字(debug 图;功能等价即可)
# ---------------------------------------------------------------------------

def rectangle(img, pt1, pt2, color, thickness=1, lineType=None, shift=None):
    arr = np.asarray(img)
    pil = _to_pil(arr)
    draw = ImageDraw.Draw(pil)
    rgb = tuple(int(c) for c in color[::-1])  # cv2 BGR -> PIL RGB
    box = [int(pt1[0]), int(pt1[1]), int(pt2[0]), int(pt2[1])]
    if int(thickness) < 0:
        draw.rectangle(box, fill=rgb)
    else:
        draw.rectangle(box, outline=rgb, width=max(1, int(thickness)))
    arr[...] = np.asarray(pil.convert("RGB"))[..., ::-1] if arr.ndim == 3 else np.asarray(pil.convert("L"))
    return img


_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _hershey_font(font_scale):
    size = max(1, round(22 * float(font_scale)))
    font = _FONT_CACHE.get(size)
    if font is None:
        font = ImageFont.load_default(size=size)
        _FONT_CACHE[size] = font
    return font


def putText(img, text, org, fontFace, fontScale, color, thickness=1,
            lineType=None, bottomLeftOrigin=False):
    if fontFace != FONT_HERSHEY_SIMPLEX:
        raise _fail("putText", f"fontFace={fontFace}")
    arr = np.asarray(img)
    pil = _to_pil(arr)
    draw = ImageDraw.Draw(pil)
    rgb = tuple(int(c) for c in color[::-1])
    draw.text(
        (int(org[0]), int(org[1])),
        str(text),
        fill=rgb,
        font=_hershey_font(fontScale),
        anchor="ls",
        stroke_width=max(0, int(thickness) - 1),
    )
    if arr.ndim == 3:
        arr[...] = np.asarray(pil.convert("RGB"))[..., ::-1]
    else:
        arr[...] = np.asarray(pil.convert("L"))
    return img
