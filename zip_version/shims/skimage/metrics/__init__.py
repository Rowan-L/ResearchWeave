"""skimage.metrics 兼容层:只提供 BabelDOC 用到的 structural_similarity。

对 skimage 0.26 默认参数路径(uint8 2D 灰度图、win_size=7、uniform_filter
mode='reflect'、use_sample_covariance=True)的等价复刻,见
zip_version/tests/test_shim_parity.py 的对拍(与真 skimage 差 <1e-6,
对 detect_scanned_file 的 0.95 阈值判定毫无影响)。
"""

from __future__ import annotations

import numpy as np

__all__ = ["structural_similarity"]


def _uniform_mean_axis(x: np.ndarray, size: int, axis: int) -> np.ndarray:
    """scipy.ndimage.uniform_filter(x, size, mode='reflect') 沿单轴的等价实现。

    scipy 'reflect' 即对称延拓(np.pad 的 'symmetric'),然后做 1/size 盒均值。
    """
    pad = size // 2
    spec = [(pad, pad) if a == axis else (0, 0) for a in range(x.ndim)]
    p = np.pad(x, spec, mode="symmetric")
    c = np.cumsum(p, axis=axis, dtype=np.float64)
    zero = np.zeros_like(np.take(c, [0], axis=axis))
    stacked = np.concatenate([zero, c], axis=axis)
    hi = np.take(stacked, range(size, stacked.shape[axis]), axis=axis)
    lo = np.take(stacked, range(stacked.shape[axis] - size), axis=axis)
    return (hi - lo) / float(size)


def _uniform_mean(x: np.ndarray, size: int) -> np.ndarray:
    out = x
    for axis in range(x.ndim):
        out = _uniform_mean_axis(out, size, axis)
    return out


def structural_similarity(
    im1,
    im2,
    *,
    win_size=7,
    gradient=False,
    data_range=None,
    channel_axis=None,
    gaussian_weights=False,
    full=False,
    **kwargs,
):
    # 只实现 BabelDOC 的调用面:默认参数、2D 图、无梯度/无通道轴。
    if gradient or channel_axis is not None or gaussian_weights:
        raise NotImplementedError(
            "skimage shim: structural_similarity 仅支持 BabelDOC 的默认参数调用"
        )
    use_sample_covariance = kwargs.pop("use_sample_covariance", True)
    K1 = kwargs.pop("K1", 0.01)
    K2 = kwargs.pop("K2", 0.03)
    if kwargs:
        raise NotImplementedError(f"skimage shim: 未支持的参数 {sorted(kwargs)}")

    im1 = np.asarray(im1)
    im2 = np.asarray(im2)
    if im1.shape != im2.shape:
        raise ValueError("Input images must have the same dimensions.")
    if im1.ndim != 2:
        raise NotImplementedError("skimage shim: 仅支持 2D 灰度图")
    if win_size % 2 == 0:
        raise ValueError("Window size must be odd.")
    if win_size > min(im1.shape):
        win_size = min(im1.shape)
        if win_size % 2 == 0:
            win_size -= 1

    if data_range is None:
        if np.issubdtype(im1.dtype, np.floating):
            raise ValueError("浮点图必须显式给 data_range")
        dmax = 255 if im1.dtype == np.uint8 else int(np.iinfo(im1.dtype).max)
        data_range = float(dmax)

    im1 = im1.astype(np.float64, copy=False)
    im2 = im2.astype(np.float64, copy=False)

    np_pts = win_size**2
    cov_norm = np_pts / (np_pts - 1.0) if use_sample_covariance else 1.0

    ux = _uniform_mean(im1, win_size)
    uy = _uniform_mean(im2, win_size)
    uxx = _uniform_mean(im1 * im1, win_size)
    uyy = _uniform_mean(im2 * im2, win_size)
    uxy = _uniform_mean(im1 * im2, win_size)
    vx = cov_norm * (uxx - ux * ux)
    vy = cov_norm * (uyy - uy * uy)
    vxy = cov_norm * (uxy - ux * uy)

    C1 = (K1 * data_range) ** 2
    C2 = (K2 * data_range) ** 2
    A1 = 2 * ux * uy + C1
    A2 = 2 * vxy + C2
    B1 = ux**2 + uy**2 + C1
    B2 = vx + vy + C2
    S = (A1 * A2) / (B1 * B2)

    pad = (win_size - 1) // 2
    mssim = float(S[pad:-pad, pad:-pad].mean(dtype=np.float64))
    return (mssim, S) if full else mssim
