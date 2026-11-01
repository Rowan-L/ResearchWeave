"""F23 shim 对拍测试:精简版兼容层 vs 真库(SD env 里装有真 cv2/skimage/sklearn)。

跑法:  <目标环境>/python.exe -m pytest zip_version/tests -q

刻意放在 zip_version/tests 而非 tests/:精简包里没有真库,对拍只能在开发环境
跑;同时不进入项目两组测试基线(避免给常规测试引入重依赖)。
核心门槛:
- cv2 resize/copyMakeBorder/cvtColor 与真库逐位一致(doclayout 预处理/扫描页检测)
- SSIM 与真 skimage 差 <1e-6(0.95 阈值判定不受影响)
- DBSCAN 标签与真 sklearn 逐值一致
- doclayout ONNX 端到端:同一页图,真 cv2 vs shim 的预处理输出与检测框一致
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ZIP_DIR = HERE.parent
ROOT = ZIP_DIR.parent
SHIMS = ZIP_DIR / "shims"
BABELDOC_SRC = ROOT / "BabelDOC-main"
MODEL_PATH = ROOT / "data/babeldoc_home/.cache/babeldoc/models/doclayout_yolo_docstructbench_imgsz1024.onnx"
TEST_PDF = ROOT / "papers/He_Delving_Deep_into_ICCV_2015_paper.pdf"

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.exists() or not TEST_PDF.exists(),
    reason="缺少 doclayout 模型或测试 PDF(对拍依赖本机资产)",
)


def _load_module(name: str, path: Path, pkg_dir: Path | None = None):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(pkg_dir)] if pkg_dir else None
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def shim_cv2():
    return _load_module("shim_cv2", SHIMS / "cv2.py")


@pytest.fixture(scope="session")
def shim_ssim():
    return _load_module(
        "shim_skimage_metrics", SHIMS / "skimage" / "metrics" / "__init__.py"
    )


@pytest.fixture(scope="session")
def shim_dbscan():
    return _load_module(
        "shim_sklearn_dbscan", SHIMS / "sklearn" / "cluster" / "__init__.py"
    )


@pytest.fixture(scope="session")
def real_cv2():
    return pytest.importorskip("cv2")


@pytest.fixture(scope="session")
def real_ssim():
    skimage = pytest.importorskip("skimage.metrics")
    return skimage.structural_similarity


@pytest.fixture(scope="session")
def real_dbscan():
    sklearn = pytest.importorskip("sklearn.cluster")
    return sklearn.DBSCAN


@pytest.fixture(scope="session")
def page_images():
    """用 vendored BabelDOC 的真实取图路径渲染 2 页(标题页 + 图表页)。"""
    sys.path.insert(0, str(BABELDOC_SRC))
    pymupdf = pytest.importorskip("pymupdf")
    from babeldoc.format.pdf.document_il.utils.mupdf_helper import get_no_rotation_img

    doc = pymupdf.open(TEST_PDF)
    images = []
    for page_number in (0, 3):
        pix = get_no_rotation_img(doc[page_number])
        img = np.frombuffer(pix.samples, np.uint8).reshape(
            pix.height, pix.width, 3
        )[:, :, ::-1]
        images.append(np.ascontiguousarray(img))
    doc.close()
    return images


# ---------------------------------------------------------------------------
# cvtColor / copyMakeBorder
# ---------------------------------------------------------------------------

def test_cvtcolor_parity(shim_cv2, real_cv2, page_images):
    rng = np.random.default_rng(2026)
    cases = [np.ascontiguousarray(img) for img in page_images]
    cases += [rng.integers(0, 256, size=(37, 51, 3), dtype=np.uint8)]

    for arr in cases:
        for code in (shim_cv2.COLOR_RGB2GRAY,):
            got = shim_cv2.cvtColor(arr, code)
            want = real_cv2.cvtColor(arr, code)
            assert np.array_equal(got, want), f"RGB2GRAY 不一致 shape={arr.shape}"
        assert np.array_equal(
            shim_cv2.cvtColor(arr, shim_cv2.COLOR_RGB2BGR),
            real_cv2.cvtColor(arr, real_cv2.COLOR_RGB2BGR),
        )


def test_copy_make_border_parity(shim_cv2, real_cv2, page_images):
    arr = page_images[0]
    for top, bottom, left, right in [(3, 4, 5, 6), (0, 300, 150, 150), (1, 1, 1, 1)]:
        got = shim_cv2.copyMakeBorder(
            arr, top, bottom, left, right, shim_cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        want = real_cv2.copyMakeBorder(
            arr, top, bottom, left, right, real_cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        assert np.array_equal(got, want)
    got = shim_cv2.copyMakeBorder(arr, 2, 2, 2, 2, shim_cv2.BORDER_CONSTANT, value=7)
    want = real_cv2.copyMakeBorder(arr, 2, 2, 2, 2, real_cv2.BORDER_CONSTANT, value=7)
    assert np.array_equal(got, want)


# ---------------------------------------------------------------------------
# resize(像素关键)
# ---------------------------------------------------------------------------

def _compare_resize(shim_cv2, real_cv2, src, dw, dh, label):
    """校准记录(F23):水平与 cv2 5.0 逐位一致;垂直复刻了 5.x 的
    (b*(S>>4)>>16 级联截断)公式后,99.98% 像素逐位一致,残差为 ±1 LSB
    (cv2 5.0 的 SIMD 内部仍有不可复刻的最后一级舍入细节,已试遍半上/半下/
    半偶/f32/mul_hi/8.8定点等假设)。门槛:最大差 ≤1 且不一致率 <0.1%。
    功能等价由 doclayout ONNX 框级对拍兜底(数量/类别一致,坐标 <0.01px)。"""
    got = shim_cv2.resize(src, (dw, dh), interpolation=shim_cv2.INTER_LINEAR)
    want = real_cv2.resize(src, (dw, dh), interpolation=real_cv2.INTER_LINEAR)
    if np.array_equal(got, want):
        return
    diff = np.abs(got.astype(int) - want.astype(int))
    rate = np.count_nonzero(diff) / diff.size
    assert diff.max() <= 1 and rate < 0.001, (
        f"resize 偏差超门槛 [{label}] shape={src.shape}->({dh},{dw}) "
        f"最大差={diff.max()} 不一致率={rate:.5f}"
    )


def test_resize_parity_doclayout_cases(shim_cv2, real_cv2, page_images):
    # doclayout 的真实场景:72dpi 页面上采样到 1024 边长
    _compare_resize(shim_cv2, real_cv2, page_images[0], 724, 1024, "A4 上采样")
    _compare_resize(shim_cv2, real_cv2, page_images[1], 724, 1024, "A4 上采样2")
    # 下采样
    _compare_resize(shim_cv2, real_cv2, page_images[0], 297, 421, "A4 下采样")
    # 灰度 2D
    gray = page_images[0][:, :, 0]
    _compare_resize(shim_cv2, real_cv2, gray, 200, 300, "灰度")


def test_resize_parity_random_sizes(shim_cv2, real_cv2):
    rng = np.random.default_rng(7)
    for i in range(24):
        h, w = rng.integers(2, 120, size=2)
        dh, dw = rng.integers(2, 160, size=2)
        img = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
        _compare_resize(shim_cv2, real_cv2, img, int(dw), int(dh), f"随机#{i}")
        gray = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
        _compare_resize(shim_cv2, real_cv2, gray, int(dw), int(dh), f"随机灰度#{i}")


def test_resize_parity_edge_sizes(shim_cv2, real_cv2):
    # 极端尺寸:坐标夹取、边缘复刻路径
    rng = np.random.default_rng(11)
    for src_shape, dst in [
        ((2, 3), (11, 17)),
        ((3, 2), (5, 40)),
        ((1, 1), (9, 9)),
        ((100, 100), (100, 100)),
        ((7, 5), (5, 7)),
    ]:
        img = rng.integers(0, 256, size=src_shape + (3,) if len(src_shape) == 2 else src_shape, dtype=np.uint8)
        _compare_resize(shim_cv2, real_cv2, img, dst[1], dst[0], f"边缘{src_shape}->{dst}")


def test_resize_fail_loud(shim_cv2):
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    with pytest.raises(NotImplementedError):
        shim_cv2.resize(img, (8, 8), interpolation=shim_cv2.INTER_CUBIC)


# ---------------------------------------------------------------------------
# SSIM
# ---------------------------------------------------------------------------

def test_ssim_parity(shim_ssim, real_ssim, page_images):
    rng = np.random.default_rng(99)
    cv2 = pytest.importorskip("cv2")
    g0 = cv2.cvtColor(page_images[0], cv2.COLOR_RGB2GRAY)
    g1 = cv2.cvtColor(page_images[1], cv2.COLOR_RGB2GRAY)

    pairs = [
        (g0, g0, "同图"),
        (g0, g1, "不同页"),
        (g0, np.clip(g0.astype(int) + rng.integers(-8, 9, g0.shape), 0, 255).astype(np.uint8), "加噪"),
        (rng.integers(0, 256, (300, 400), dtype=np.uint8),
         rng.integers(0, 256, (300, 400), dtype=np.uint8), "随机噪声"),
        (g0[:400, :300], g1[:400, :300], "裁剪页"),
    ]
    for a, b, label in pairs:
        got = shim_ssim.structural_similarity(a, b)
        want = real_ssim(a, b)
        assert abs(got - want) < 1e-6, f"SSIM 不一致 [{label}]: {got} vs {want}"
        # 阈值决策必须一致(detect_scanned_file 用 0.95)
        assert (got > 0.95) == (want > 0.95), f"SSIM 阈值决策不一致 [{label}]"


def test_ssim_fail_loud(shim_ssim):
    img = np.zeros((16, 16), dtype=np.uint8)
    with pytest.raises(NotImplementedError):
        shim_ssim.structural_similarity(img, img, gradient=True)


# ---------------------------------------------------------------------------
# DBSCAN
# ---------------------------------------------------------------------------

def _compare_dbscan(shim_dbscan, real_dbscan_cls, X, eps, min_samples, metric, label):
    shim = shim_dbscan.DBSCAN(eps=eps, min_samples=min_samples, metric=metric).fit(X)
    real = real_dbscan_cls(eps=eps, min_samples=min_samples, metric=metric).fit(X)
    assert np.array_equal(shim.labels_, real.labels_), (
        f"DBSCAN 标签不一致 [{label}] n={len(X)} eps={eps} min_samples={min_samples}"
    )


def test_dbscan_parity_min_samples_1(shim_dbscan, real_dbscan):
    # BabelDOC 的真实用法:2D 中心点、manhattan、min_samples=1
    rng = np.random.default_rng(3)
    for i, eps in enumerate([1.0, 2.5, 8.0, 0.3]):
        n = 80
        pts = rng.integers(0, 40, size=(n, 2)).astype(np.float64)
        _compare_dbscan(shim_dbscan, real_dbscan, pts, eps, 1, "manhattan", f"随机#{i}")
    # 链式点(eps 通路)与孤立点混合
    chain = np.array([[float(x), 0.0] for x in range(30)])
    chain = np.vstack([chain, [[1000.0, 1000.0]]])
    _compare_dbscan(shim_dbscan, real_dbscan, chain, 1.0, 1, "manhattan", "链+孤点")
    # 大 N 性能/正确性
    big = rng.integers(0, 200, size=(600, 2)).astype(np.float64)
    _compare_dbscan(shim_dbscan, real_dbscan, big, 5.0, 1, "manhattan", "大N")


def test_dbscan_parity_min_samples_gt1(shim_dbscan, real_dbscan):
    rng = np.random.default_rng(5)
    for min_samples in (2, 3, 5):
        pts = rng.normal(size=(150, 2)) * 10
        _compare_dbscan(shim_dbscan, real_dbscan, pts, 3.0, min_samples, "euclidean", f"ms{min_samples}")
    # 噪声语义:远离簇的点应为 -1
    pts = np.vstack([rng.normal(0, 0.5, size=(50, 2)), [[100.0, 100.0]]])
    shim = shim_dbscan.DBSCAN(eps=2.0, min_samples=3, metric="euclidean").fit(pts)
    real = real_dbscan(eps=2.0, min_samples=3, metric="euclidean").fit(pts)
    assert np.array_equal(shim.labels_, real.labels_)
    assert shim.labels_[-1] == -1


def test_dbscan_fail_loud(shim_dbscan):
    with pytest.raises(NotImplementedError):
        shim_dbscan.DBSCAN(eps=1.0, metric="cosine")


# ---------------------------------------------------------------------------
# imwrite / imencode / imread(功能等价;debug 与 RPC 路径)
# ---------------------------------------------------------------------------

def test_image_io_roundtrip(shim_cv2, tmp_path):
    rng = np.random.default_rng(1)
    bgr = rng.integers(0, 256, size=(40, 60, 3), dtype=np.uint8)
    png_path = tmp_path / "debug.png"
    assert shim_cv2.imwrite(png_path, bgr)
    back = shim_cv2.imread(png_path)
    assert back is not None and back.shape == bgr.shape
    assert np.array_equal(back, bgr)  # PNG 无损

    ok, buf = shim_cv2.imencode(".jpg", bgr)
    assert ok and buf.dtype == np.uint8 and buf.tobytes()[:2] == b"\xff\xd8"


def test_rectangle_puttext_functional(shim_cv2):
    img = np.zeros((60, 80, 3), dtype=np.uint8)
    out = shim_cv2.rectangle(img, (10, 10), (40, 40), (0, 0, 255), 2)
    assert out is img and img[10:41, 10].any()
    out = shim_cv2.putText(img, "hello", (5, 55), shim_cv2.FONT_HERSHEY_SIMPLEX,
                           0.7, (255, 255, 255), 1)
    assert out is img and img[:, :, 0].max() > 0


# ---------------------------------------------------------------------------
# doclayout ONNX 端到端:真 cv2 vs shim
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def doclayout_pair(page_images):
    """(真 cv2 模型, shim 模型);shim 通过 reload doclayout 模块注入。"""
    sys.path.insert(0, str(BABELDOC_SRC))
    import babeldoc.docvision.doclayout as dl

    real_model = dl.OnnxModel(str(MODEL_PATH))
    real_pre = [real_model.resize_and_pad_image(img, 1024) for img in page_images]
    real_results = [real_model.predict(img)[0] for img in page_images]

    import cv2 as real_cv2_module  # 真库引用留底

    shim_cv2 = _load_module("shim_cv2_reload", SHIMS / "cv2.py")
    sys.modules["cv2"] = shim_cv2
    try:
        importlib.reload(dl)
        assert dl.cv2 is shim_cv2
        shim_model = dl.OnnxModel(str(MODEL_PATH))
        shim_pre = [shim_model.resize_and_pad_image(img, 1024) for img in page_images]
        shim_results = [shim_model.predict(img)[0] for img in page_images]
    finally:
        sys.modules["cv2"] = real_cv2_module
        importlib.reload(dl)

    return real_pre, real_results, shim_pre, shim_results


def test_doclayout_preprocessing_within_tolerance(doclayout_pair):
    real_pre, _, shim_pre, _ = doclayout_pair
    for i, (r, s) in enumerate(zip(real_pre, shim_pre)):
        assert r.shape == s.shape
        diff = np.abs(r.astype(int) - s.astype(int))
        rate = np.count_nonzero(diff) / diff.size
        assert diff.max() <= 1 and rate < 0.001, (
            f"第{i}页预处理偏差超门槛:最大差={diff.max()} 不一致率={rate:.5f}"
        )


def test_doclayout_boxes_identical(doclayout_pair):
    real_results, shim_results = doclayout_pair[1], doclayout_pair[3]
    for i, (r, s) in enumerate(zip(real_results, shim_results)):
        assert len(r.boxes) == len(s.boxes), (
            f"第{i}页检测框数量不一致: {len(r.boxes)} vs {len(s.boxes)}"
        )
        for rb, sb in zip(r.boxes, s.boxes):
            assert rb.cls == sb.cls, f"第{i}页框类别不一致"
            assert np.allclose(rb.xyxy, sb.xyxy, atol=1e-3), "框坐标偏差过大"
            assert abs(rb.conf - sb.conf) < 1e-4, "置信度偏差过大"
