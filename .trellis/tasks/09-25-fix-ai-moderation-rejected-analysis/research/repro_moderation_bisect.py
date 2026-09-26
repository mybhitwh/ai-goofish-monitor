#!/usr/bin/env python3
"""受控复现：定位百炼 data_inspection_failed 的触发条件（2026-09-25 实测脚本归档）。

用法（在仓库根，用 .venv）：
    .venv/bin/python .trellis/tasks/09-25-fix-ai-moderation-rejected-analysis/research/repro_moderation_bisect.py --case all

依赖 .env 中的 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL_NAME（点此脚本不会打印密钥）。
图片从商品快照里的闲鱼 CDN 链接现场下载并缓存到 --img-dir。

实测结论见同目录 dashscope-moderation-evidence.md。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from PIL import Image  # noqa: E402

from src.ai_message_builder import (  # noqa: E402
    build_analysis_text_prompt,
    build_user_message_content,
)
from src.services.ai_request_compat import (  # noqa: E402
    CHAT_COMPLETIONS_API_MODE,
    build_ai_request_params,
)

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "product-1086957165721-snapshot.json"
DEFAULT_PROMPT = REPO / "prompts" / "ipad_air_m4_criteria.txt"


def load_inputs(prompt_file: Path) -> tuple[str, list[str]]:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    product_json = json.dumps(snapshot["product_data"], ensure_ascii=False, indent=2)
    urls = snapshot["product_data"]["商品信息"]["商品图片列表"]
    prompt_text = prompt_file.read_text(encoding="utf-8")
    return product_json, prompt_text, urls


def fetch_images(urls: list[str], img_dir: Path) -> list[Path]:
    img_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, url in enumerate(urls, 1):
        target = img_dir / f"p{i}_{os.path.basename(url)[:12]}.jpg"
        if not target.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                target.write_bytes(resp.read())
        paths.append(target)
    return paths


def b64_raw(path: Path) -> str:
    """线上口径：原始字节，不做任何转码。"""
    return base64.b64encode(path.read_bytes()).decode()


def b64_jpeg(path: Path, quality: int, scale: float) -> str:
    image = Image.open(path).convert("RGB")
    if scale != 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def describe(path: Path) -> str:
    image = Image.open(path)
    return f"{image.format} {image.size} {path.stat().st_size}B"


def build_params(model: str, product_json: str, prompt_text: str, data_urls: list[str]) -> dict:
    text_prompt = build_analysis_text_prompt(
        product_json, prompt_text, include_images=bool(data_urls)
    )
    content = build_user_message_content(text_prompt, data_urls)
    return build_ai_request_params(
        CHAT_COMPLETIONS_API_MODE,
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=0.1,
        max_output_tokens=4000,
        enable_json_output=True,
    )


async def call(client: AsyncOpenAI, label: str, params: dict) -> str:
    mb = len(json.dumps(params).encode()) / 1024 / 1024
    try:
        await client.chat.completions.create(**params)
        verdict = "通过"
    except Exception as exc:  # noqa: BLE001 - 复现脚本需要看到全部错误类型
        text = str(exc)
        if "data_inspection_failed" in text:
            verdict = "审核拦截 data_inspection_failed"
        elif "file size is too large" in text:
            verdict = "体积超限 Multimodal file size is too large"
        else:
            verdict = f"其他错误: {text[:160]}"
    print(f"[{label}] {verdict}   payload={mb:6.2f}MB", flush=True)
    return verdict


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        default="all",
        choices=["all", "text", "raw", "scaled", "jpeg", "ladder", "single", "mime"],
    )
    parser.add_argument("--img-dir", default="/tmp/repro_moderation_imgs", type=Path)
    parser.add_argument("--prompt-file", default=DEFAULT_PROMPT, type=Path)
    parser.add_argument("--quality", default=60, type=int, help="--case jpeg 用的 JPEG 质量")
    args = parser.parse_args()

    load_dotenv(dotenv_path=REPO / ".env", override=True)
    model = os.environ["OPENAI_MODEL_NAME"]
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"])

    product_json, prompt_text, urls = load_inputs(args.prompt_file)
    images = fetch_images(urls, args.img_dir)
    print(f"模型={model}")
    for p in images:
        print(f"  图片 {p.name}: {describe(p)}")
    print()

    def params_for(maker) -> dict:
        return build_params(model, product_json, prompt_text, maker())

    cases = {
        "text": [("纯文本", lambda: params_for(lambda: []))],
        "raw": [
            (
                "线上原始口径（原始字节声明 jpeg）",
                lambda: params_for(lambda: [f"data:image/jpeg;base64,{b64_raw(p)}" for p in images]),
            )
        ],
        "scaled": [
            (
                "缩到 30% 后转真 JPEG",
                lambda: params_for(lambda: [f"data:image/jpeg;base64,{b64_jpeg(p, 85, 0.3)}" for p in images]),
            )
        ],
        "jpeg": [
            (
                f"原分辨率转真 JPEG q{args.quality}",
                lambda: params_for(
                    lambda: [f"data:image/jpeg;base64,{b64_jpeg(p, args.quality, 1.0)}" for p in images]
                ),
            )
        ],
        "ladder": [
            (
                f"阶梯 q{q} 原分辨率",
                lambda q=q: params_for(
                    lambda: [f"data:image/jpeg;base64,{b64_jpeg(p, q, 1.0)}" for p in images]
                ),
            )
            for q in (45, 50, 55, 60, 90, 100)
        ],
        "single": [
            (
                f"单图 {p.name}（原始字节声明 jpeg）",
                lambda p=p: params_for(lambda: [f"data:image/jpeg;base64,{b64_raw(p)}"]),
            )
            for p in images
        ],
        "mime": [
            (
                "WebP 字节 + 声明 image/webp",
                lambda: params_for(lambda: [f"data:image/webp;base64,{b64_raw(p)}" for p in images]),
            ),
            (
                "JPEG 字节 + 声明 image/webp",
                lambda: params_for(
                    lambda: [f"data:image/webp;base64,{b64_jpeg(p, args.quality, 1.0)}" for p in images]
                ),
            ),
        ],
    }

    selected = ["text", "raw", "scaled", "jpeg", "ladder", "single", "mime"] if args.case == "all" else [args.case]
    for name in selected:
        print(f"--- case: {name} ---")
        for label, factory in cases[name]:
            await call(client, label, factory())
        print()


if __name__ == "__main__":
    asyncio.run(main())