import argparse
import asyncio
import importlib.util
import re
import sys
import types
from pathlib import Path

from bilibili_api.opus import Opus
from msgspec import convert


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BILIBILI_DIR = PROJECT_ROOT / "core" / "parsers" / "bilibili"


def _prepare_package_context() -> None:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str(PROJECT_ROOT / "core")]
    sys.modules.setdefault("core", core_pkg)

    parsers_pkg = types.ModuleType("core.parsers")
    parsers_pkg.__path__ = [str(PROJECT_ROOT / "core" / "parsers")]
    sys.modules.setdefault("core.parsers", parsers_pkg)

    bili_pkg = types.ModuleType("core.parsers.bilibili")
    bili_pkg.__path__ = [str(BILIBILI_DIR)]
    sys.modules.setdefault("core.parsers.bilibili", bili_pkg)


def _load_module(fullname: str, path: Path):
    spec = importlib.util.spec_from_file_location(fullname, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


def load_opus_item_class():
    _prepare_package_context()
    opus_module = _load_module("core.parsers.bilibili.opus", BILIBILI_DIR / "opus.py")
    return opus_module.OpusItem


def extract_opus_id(text: str) -> int:
    match = re.search(r"bilibili\.com/opus/(\d+)", text)
    if not match:
        raise ValueError("无法从输入中提取 opus_id，请传入 bilibili.com/opus/... 链接")
    return int(match.group(1))


async def run_min_test(opus_id: int) -> None:
    OpusItem = load_opus_item_class()

    opus = Opus(opus_id)
    raw_info = await opus.get_info()
    data = convert(raw_info, OpusItem)

    print("=== OpusItem 基础字段 ===")
    print(f"id_str: {data.item.id_str}")
    print(f"title: {data.title}")
    print(f"name_avatar: {data.name_avatar}")
    print(f"timestamp: {data.timestamp}")

    print("\n=== opus.py 关键生成器 gen_text_img() ===")
    node_count = 0
    text_count = 0
    image_count = 0
    for node in data.gen_text_img():
        node_count += 1
        if node.__class__.__name__ == "TextNode":
            text_count += 1
        elif node.__class__.__name__ == "ImageNode":
            image_count += 1

        if node_count <= 5:
            if hasattr(node, "text"):
                preview = getattr(node, "text", "")[:80].replace("\n", " ")
                print(f"[{node_count}] TextNode: {preview}")
            else:
                print(f"[{node_count}] ImageNode: {getattr(node, 'url', '')}")

    print(f"\nnode_count={node_count}, text_count={text_count}, image_count={image_count}")
    print("✅ 最小测试通过：opus.py 的模型转换与图文节点生成可正常调用")


def main() -> None:
    parser = argparse.ArgumentParser(description="最小化测试 core/parsers/bilibili/opus.py")
    parser.add_argument(
        "--url",
        default="https://www.bilibili.com/opus/1160172882624512000",
        help="opus 链接，默认使用当前会话提供的链接",
    )
    args = parser.parse_args()

    opus_id = extract_opus_id(args.url)
    asyncio.run(run_min_test(opus_id))


if __name__ == "__main__":
    main()
