import argparse
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

from bilibili_api.video import Video
from msgspec import convert

# 兼容直接运行：将项目根目录加入模块搜索路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BILIBILI_DIR = PROJECT_ROOT / "core" / "parsers" / "bilibili"

# 构造最小包上下文，避免执行 core/parsers/__init__.py 的重依赖导入
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


_load_module("core.parsers.bilibili.common", BILIBILI_DIR / "common.py")
video_module = _load_module("core.parsers.bilibili.video", BILIBILI_DIR / "video.py")
VideoInfo = video_module.VideoInfo


def _print_dict_tree(data: object, prefix: str = "", max_depth: int = 2, depth: int = 0) -> None:
    if depth > max_depth:
        return

    if isinstance(data, dict):
        for key, value in data.items():
            type_name = type(value).__name__
            print(f"{prefix}{key} ({type_name})")
            if isinstance(value, dict):
                _print_dict_tree(value, prefix + "  ", max_depth, depth + 1)
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                print(f"{prefix}  [0]")
                _print_dict_tree(value[0], prefix + "    ", max_depth, depth + 1)


async def run_min_test(
    bvid: str,
    page: int,
    inspect_fields: bool,
    tree_depth: int,
) -> None:
    video = Video(bvid=bvid)
    raw_info = await video.get_info()
    info = convert(raw_info, VideoInfo)

    page_1 = info.extract_info_with_page(1)
    page_target = info.extract_info_with_page(page)

    print("=== VideoInfo 基础字段 ===")
    print(f"bvid: {info.bvid}")
    print(f"title: {info.title}")
    print(f"owner: {info.owner.name}")
    print(f"duration: {info.duration}s")
    print(f"pubdate: {info.pubdate}")

    print("\n=== video.py 关键属性 ===")
    print(f"title_with_part: {info.title_with_part}")
    print(f"formatted_stats_info: {info.formatted_stats_info}")

    print("\n=== extract_info_with_page(1) ===")
    print(
        f"index={page_1.index}, title={page_1.title}, duration={page_1.duration}, "
        f"timestamp={page_1.timestamp}, cover={page_1.cover}"
    )

    print(f"\n=== extract_info_with_page({page}) ===")
    print(
        f"index={page_target.index}, title={page_target.title}, duration={page_target.duration}, "
        f"timestamp={page_target.timestamp}, cover={page_target.cover}"
    )

    if inspect_fields:
        print("\n=== 原始字段探查（用于挑选可新增字段）===")
        print(f"top-level keys: {sorted(raw_info.keys())}")

        stat_keys = sorted(raw_info.get("stat", {}).keys()) if isinstance(raw_info.get("stat"), dict) else []
        owner_keys = sorted(raw_info.get("owner", {}).keys()) if isinstance(raw_info.get("owner"), dict) else []
        pages = raw_info.get("pages")
        page0_keys = sorted(pages[0].keys()) if isinstance(pages, list) and pages and isinstance(pages[0], dict) else []

        print(f"stat keys: {stat_keys}")
        print(f"owner keys: {owner_keys}")
        print(f"pages[0] keys: {page0_keys}")

        print(f"\n=== 字段树（max_depth={tree_depth}）===")
        _print_dict_tree(raw_info, max_depth=max(1, tree_depth))

    print("\n✅ 最小测试通过：video.py 的模型转换与属性方法可正常调用")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="最小化测试 core/parsers/bilibili/video.py 的数据结构与属性方法"
    )
    parser.add_argument("--bvid", required=True, help="要测试的 BV 号，例如 BV19cAkzYE89")
    parser.add_argument("--page", type=int, default=1, help="要测试的分P，默认 1")
    parser.add_argument(
        "--inspect-fields",
        action="store_true",
        help="打印原始接口字段键名（含 stat/owner/pages）",
    )
    parser.add_argument(
        "--tree-depth",
        type=int,
        default=2,
        help="字段树展开深度，默认 2",
    )
    args = parser.parse_args()

    asyncio.run(
        run_min_test(
            args.bvid,
            max(1, args.page),
            args.inspect_fields,
            args.tree_depth,
        )
    )


if __name__ == "__main__":
    main()
