"""F23 精简版打包端到端验证:用精简包的 ResearchWeaveBabelDOC.exe 跑真实翻译。

验证点:
1. 冻结环境里 runtime hook 生效,`import cv2/skimage/sklearn` 命中 shims;
2. doclayout(cv2 shim)对真实论文页输出版面框并完成翻译;
3. detect_scanned(SSIM shim)在每页正常运行;
4. lxml 排除后 xsdata 原生解析器可用;scipy/sklearn 排除后 extract_char 正常;
5. BabelDOC 本地补丁 token_usage.json 正常落盘(vendored 代码进包的证据)。

用法:  <目标环境>/python.exe zip_version/tests/packaged_e2e.py
产物在 zip_version/e2e_tmp/,跑完保留供检查,可手动删除。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZIP_DIR = HERE.parent
ROOT = ZIP_DIR.parent
APP = ZIP_DIR / "dist" / "ResearchWeave"
BABELDOC_EXE = APP / "ResearchWeaveBabelDOC.exe"
TMP = ZIP_DIR / "e2e_tmp"
SOURCE_PDF = ROOT / "papers" / "He_Delving_Deep_into_ICCV_2015_paper.pdf"


def main() -> int:
    assert BABELDOC_EXE.exists(), f"缺少 {BABELDOC_EXE}"

    # 1) 取打包版 API 设置(只读;不在输出里打印 key)。
    # base_url 等明文项在 data/llm_settings.json;api_key 存系统 keyring
    # (由应用的 settings 模块管理),此处仅读入内存。
    settings = json.loads(
        (ROOT / "dist/ResearchWeave/data/llm_settings.json").read_text(encoding="utf-8")
    )
    base_url = settings.get("base_url", "")
    model = settings.get("model", "")
    auth_style = settings.get("api_auth_style", "Authorization")
    sys.path.insert(0, str(ROOT / "src"))
    from synthrecall.settings import get_llm_api_key

    api_key = get_llm_api_key(base_url)
    assert api_key, "keyring 里没有该 base_url 的 API key,无法做真实翻译验证"
    print(f"API 设置: base_url={base_url} model={model} auth={auth_style} key=<已载入>")

    # 2) 组装临时目录
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "out").mkdir(parents=True)
    work_pdf = TMP / "input2p.pdf"

    import pymupdf

    src = pymupdf.open(SOURCE_PDF)
    dst = pymupdf.open()
    dst.insert_pdf(src, from_page=0, to_page=1)
    dst.save(work_pdf)
    dst.close(); src.close()
    print(f"测试 PDF: {work_pdf} ({work_pdf.stat().st_size/1024:.0f}KB, 前2页)")

    # 3) 预拷 babeldoc assets(避免首跑联网下载)
    home = TMP / "home"
    src_cache = ROOT / "data/babeldoc_home/.cache/babeldoc"
    tgt_cache = home / ".cache" / "babeldoc"
    tgt_cache.mkdir(parents=True)
    for item in src_cache.iterdir():
        if item.is_dir():
            shutil.copytree(item, tgt_cache / item.name)
        else:
            shutil.copy2(item, tgt_cache / item.name)
    print(f"assets 已拷贝 -> {tgt_cache}")

    # 4) 与 translation.py 完全一致的命令行
    command = [
        str(BABELDOC_EXE),
        "--openai",
        "--openai-model", model,
        "--openai-base-url", base_url,
        "--openai-api-key-header", auth_style,
        "--lang-in", "en",
        "--lang-out", "zh",
        "--qps", "2",
        "--pool-max-workers", "2",
        "--no-auto-extract-glossary",
        "--watermark-output-mode", "no_watermark",
        "--output", str(TMP / "out"),
        "--working-dir", str(TMP / "out" / "_working"),
        "--no-dual",
        "--files", str(work_pdf),
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["SYNTHRECALL_OPENAI_API_KEY"] = api_key
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    # 注意:不设 PYTHONPATH——精简包必须只靠自带的 vendored babeldoc + shims

    print("启动 ResearchWeaveBabelDOC.exe ...")
    t0 = time.time()
    process = subprocess.Popen(
        command, cwd=str(ROOT), env=env, text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    log_tail: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        log_tail.append(line.rstrip())
        if len(log_tail) > 400:
            log_tail = log_tail[-400:]
    code = process.wait()
    elapsed = time.time() - t0
    print(f"退出码={code} 耗时={elapsed:.0f}s")

    print("\n--- 日志尾部(关键行)---")
    keywords = ("error", "Error", "ERROR", "Traceback", "ModuleNotFound",
                "ImportError", "cv2", "skimage", "sklearn", "layout", "Scanned",
                "translated", "mono", "token")
    for line in log_tail:
        if any(k in line for k in keywords):
            print("  ", line[:200])

    # 5) 产物断言
    out_dir = TMP / "out"
    mono = sorted(out_dir.glob("*.mono.pdf"))
    token_json = out_dir / "token_usage.json"
    print("\n--- 断言 ---")
    ok = True
    if mono:
        size_mb = mono[0].stat().st_size / 1024 / 1024
        print(f"PASS 译文单本: {mono[0].name} ({size_mb:.2f}MB)")
    else:
        print("FAIL 未找到 *.mono.pdf")
        ok = False
    if token_json.exists():
        data = json.loads(token_json.read_text(encoding="utf-8"))
        print(f"PASS token_usage.json: total={data.get('total_tokens')} "
              f"(prompt={data.get('prompt_tokens')}, completion={data.get('completion_tokens')})")
    else:
        print("FAIL token_usage.json 缺失(本地补丁未生效?)")
        ok = False
    joined = "\n".join(log_tail)
    for bad in ("ModuleNotFoundError", "ImportError", "No module named"):
        if bad in joined:
            print(f"FAIL 日志含 {bad}")
            ok = False
    print("\n结果:", "E2E 通过" if ok else "E2E 失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
