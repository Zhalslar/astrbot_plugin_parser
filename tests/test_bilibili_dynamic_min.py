import argparse
import asyncio
import importlib.util
import re
import sys
from pathlib import Path

from bilibili_api.dynamic import Dynamic
from msgspec import convert


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_dynamic_data_class():
    dynamic_model_path = PROJECT_ROOT / "core" / "parsers" / "bilibili" / "dynamic.py"
    spec = importlib.util.spec_from_file_location("bili_dynamic_model", dynamic_model_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {dynamic_model_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DynamicData


def extract_dynamic_id(text: str) -> int:
    match = re.search(r"(?:t\.bilibili\.com|bilibili\.com/dynamic)/(\d+)", text)
    if not match:
        raise ValueError("无法从输入中提取动态 ID，请传入 t.bilibili.com 或 bilibili.com/dynamic 链接")
    return int(match.group(1))


async def run_min_test(dynamic_id: int) -> None:
    DynamicData = load_dynamic_data_class()

    dynamic = Dynamic(dynamic_id)
    raw_info = await dynamic.get_info()
    data = convert(raw_info, DynamicData)
    item = data.item

    print("=== DynamicData 基础字段 ===")
    print(f"id_str: {item.id_str}")
    print(f"type: {item.type}")
    print(f"visible: {item.visible}")
    print(f"name: {item.name}")
    print(f"avatar: {item.avatar}")
    print(f"timestamp: {item.timestamp}")

    print("\n=== dynamic.py 关键属性 ===")
    print(f"title: {item.title}")
    print(f"text: {item.text}")
    print(f"cover_url: {item.cover_url}")
    print(f"image_urls_count: {len(item.image_urls)}")
    if item.image_urls:
        print(f"image_urls[0]: {item.image_urls[0]}")

    print("\n✅ 最小测试通过：dynamic.py 的模型转换与属性方法可正常调用")


def main() -> None:
    parser = argparse.ArgumentParser(description="最小化测试 core/parsers/bilibili/dynamic.py")
    parser.add_argument(
        "--url",
        default="https://t.bilibili.com/1170306222287487011",
        help="动态链接，默认使用当前会话提供的链接",
    )
    args = parser.parse_args()

    dynamic_id = extract_dynamic_id(args.url)
    asyncio.run(run_min_test(dynamic_id))


if __name__ == "__main__":
    main()
