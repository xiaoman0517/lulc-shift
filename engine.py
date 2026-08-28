#!/usr/bin/env python3
"""
土地覆盖变化检测处理引擎
========================
被两处复用：
  - CLI 脚本 change_detection_demo.py
  - Web API  api/index.py（Vercel 部署）

职责：
  1. 从 Planetary Computer STAC 拉取两个年份的 io-lulc-9-class 分类影像
  2. 对齐网格后逐像素比较，生成"变化编码栅格"（code = before*10 + after + 1）
  3. 把解码信息写入 GeoTIFF 元数据标签（命名空间 CHANGE_DECODE）
  4. 转矢量 GeoJSON，属性表带 from_class / to_class / transition / 面积
  5. 通过 Progress 回调向调用方上报进度（Web 端轮询展示）
"""
import io
import json
import math
import os
import sys
import time

# 注意：第三方科学计算库（numpy / rasterio / shapely / PIL / pystac_client / planetary_computer）
# 不在模块顶层导入，而是在各函数内部按需导入。
# 原因：Serverless（如 Vercel）环境中，模块加载阶段导入重型二进制库可能导致函数
# 启动崩溃（FUNCTION_INVOCATION_FAILED）。按需导入可保证首页与诊断接口始终可用。


def _ensure_proj_env():
    """确保 PROJ 使用 rasterio 自带的数据库（避免被 PostgreSQL 等第三方改写到旧版本目录）。
    用 find_spec 定位 rasterio 而不实际导入，保证可在导入 rasterio 之前设置环境变量。"""
    import importlib.util
    _spec = importlib.util.find_spec("rasterio")
    if _spec and _spec.origin:
        _rp = os.path.join(os.path.dirname(_spec.origin), "proj_data")
        if os.path.isdir(_rp):
            os.environ["PROJ_DATA"] = _rp
            os.environ["PROJ_LIB"] = _rp

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "io-lulc-9-class"

# io-lulc-9-class 是年度产品，collection 目前只覆盖 2017~2022
YEARS = list(range(2017, 2023))

# 类别编码（1-based），nodata 值为 11
CLASS_NAMES = {
    1: "水体", 2: "林地", 3: "草地", 4: "洪泛植被",
    5: "作物", 6: "灌木", 7: "建设用地", 8: "裸地", 9: "雪/冰",
}
NODATA = 11

# 类别渲染颜色（与 Dynamic World / io-lulc 官方配色一致，RGB）
CLASS_COLORS = {
    1: (65, 155, 223),      # 水体 水蓝
    2: (57, 125, 73),       # 林地 深绿
    3: (136, 176, 83),      # 草地 浅绿
    4: (122, 135, 198),     # 洪泛植被 蓝紫
    5: (228, 150, 53),      # 作物 橙黄
    6: (223, 195, 90),      # 灌木 黄
    7: (196, 40, 27),       # 建设用地 红
    8: (165, 155, 143),     # 裸地 棕灰
    9: (179, 159, 225),     # 雪/冰 淡紫
}

# 变化类型调色板（Tab20，瓦片与前端 GeoJSON 共用同一套规则）
TAB20_HEX = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
]

def change_color_for_code(code):
    """变化编码 -> RGB（按 code 排序后分配 Tab20，保证与前端/GeoJSON 一致）"""
    # 排序依据：按转移类型稳定排序（用 code 从小到大即可，均匀取色）
    idx = int(code) % len(TAB20_HEX)
    c = TAB20_HEX[idx].lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


class Progress:
    """进度上报：percent 0~100，logs 为消息列表，sink 为外部回调（如写任务状态）"""

    def __init__(self, sink=None):
        self.percent = 0
        self.logs = []
        self.sink = sink

    def update(self, percent, message):
        self.percent = int(max(0, min(100, percent)))
        self.logs.append({"at": time.strftime("%H:%M:%S"), "message": message})
        if self.sink:
            try:
                self.sink(self)
            except Exception:
                pass


def decode(code):
    """变化编码 -> (before, after)；0 表示无变化，返回 None"""
    if code == 0:
        return None
    return (code - 1) // 10, (code - 1) % 10


def encode(before, after):
    """类别对 -> 变化编码"""
    return before * 10 + after + 1


def _pick_item(items, bbox):
    """io-lulc 按 UTM 分带组织瓦片，选择 bbox 中心点所在的那一景"""
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    for it in items:
        b = it.bbox
        if b and b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            return it
    return items[0]


def fetch_lulc_array(catalog, bbox, date_range, progress=None):
    """搜索 + 裁剪读取一个时相的分类影像，返回 (数组, transform, crs, profile, item_id)"""
    import rasterio
    from rasterio.warp import transform_bounds
    import planetary_computer as pc

    search = catalog.search(
        collections=[COLLECTION],
        bbox=bbox,
        datetime=date_range,
    )
    items = list(search.item_collection())
    if not items:
        raise RuntimeError(f"在 {date_range} 范围内没搜到任何 {COLLECTION} 数据，检查 BBOX 或日期范围")

    item = pc.sign(_pick_item(items, bbox))
    asset_href = item.assets["data"].href

    if progress:
        progress.update(progress.percent, f"读取影像 {item.id} ...")

    with rasterio.open(asset_href) as src:
        # io-lulc 的 COG 是 UTM 投影（米），bbox 是经纬度，先转换到栅格坐标系再裁剪
        dst_bounds = transform_bounds("EPSG:4326", src.crs, *bbox)
        window = rasterio.windows.from_bounds(*dst_bounds, transform=src.transform)
        window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        if window.width <= 0 or window.height <= 0:
            raise RuntimeError("裁剪窗口为空，请检查 BBOX 是否在数据覆盖范围内")
        arr = src.read(1, window=window)
        transform = src.window_transform(window)
        crs = src.crs
        profile = src.profile.copy()

    return arr, transform, crs, profile, item.id


def align_to_reference(arr, src_transform, src_crs, ref_arr, ref_transform, ref_crs):
    """把第二期影像重投影/重采样对齐到第一期的网格，保证逐像素可比（分类图必须用最近邻）"""
    import numpy as np
    from rasterio.warp import reproject, Resampling

    aligned = np.zeros_like(ref_arr)
    reproject(
        source=arr,
        destination=aligned,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=ref_transform,
        dst_crs=ref_crs,
        resampling=Resampling.nearest,
    )
    return aligned


def summarize_transitions(before, after):
    """统计类别转移，排除 nodata，返回 (mask, { (before,after): 像素数 })"""
    mask = (before != after) & (before != NODATA) & (after != NODATA)
    changed_from = before[mask]
    changed_to = after[mask]
    transitions = {}
    for f, t in zip(changed_from, changed_to):
        key = (int(f), int(t))
        transitions[key] = transitions.get(key, 0) + 1
    return mask, transitions

def write_tif_with_metadata(change_code, profile, transform, crs, path):
    """写变化栅格，并把完整解码表写入 GeoTIFF 元数据（命名空间 CHANGE_DECODE）"""
    import rasterio

    profile.update({
        "height": change_code.shape[0],
        "width": change_code.shape[1],
        "transform": transform,
        "crs": crs,
        "dtype": "int16",
        "count": 1,
        "compress": "deflate",
    })
    # 解码表：0=无变化，其余为 9 类之间所有理论转移
    decode_tags = {"0": "无变化"}
    for b in range(1, 10):
        for a in range(1, 10):
            if b != a:
                decode_tags[str(encode(b, a))] = f"{CLASS_NAMES[b]} -> {CLASS_NAMES[a]}"

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(change_code, 1)
        dst.update_tags(ns="CHANGE_DECODE", **decode_tags)
        dst.update_tags(
            collection=COLLECTION,
            generation="land-cover-analysis demo",
            description="变化编码：0=无变化；code=before*10+after+1，类别见 CHANGE_DECODE 标签",
        )
    return path



def write_class_tif(arr, profile, transform, crs, path):
    """写一个时相的分类栅格（uint8，nodata=11），供前端瓦片叠加显示"""
    import rasterio

    profile.update({
        "height": arr.shape[0],
        "width": arr.shape[1],
        "transform": transform,
        "crs": crs,
        "dtype": "uint8",
        "count": 1,
        "compress": "deflate",
        "nodata": NODATA,
    })
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr.astype("uint8"), 1)
        dst.update_tags(collection=COLLECTION, nodata_value=str(NODATA))
    return path


def render_tile(tif_path, z, x, y, kind="class", tile_size=256):
    """把任意 GeoTIFF 渲染成 Web-Mercator XYZ 瓦片 PNG。

    kind:
      - "class" : 分类栅格（before/after），用 CLASS_COLORS 渲染，nodata 透明
      - "change": 变化栅格，用 Tab20 调色板渲染，0=无变化 透明
    返回 PNG 字节；范围外返回 256x256 全透明 PNG。
    """
    _ensure_proj_env()

    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds, Resampling
    from PIL import Image

    n = 2 ** z
    if not (0 <= int(x) < n and 0 <= int(y) < n):
        return _empty_png(tile_size)

    # Web Mercator tile bounds（米）
    r = 6378137.0
    tile_span = 2 * math.pi * r / n
    west = -math.pi * r + x * tile_span
    east = west + tile_span
    north = math.pi * r - y * tile_span
    south = north - tile_span

    try:
        with rasterio.open(tif_path) as src:
            b = transform_bounds("EPSG:3857", src.crs, west, south, east, north)
            window = rasterio.windows.from_bounds(*b, transform=src.transform)
            window = window.intersection(
                rasterio.windows.Window(0, 0, src.width, src.height))
            if window.width <= 0 or window.height <= 0:
                return _empty_png(tile_size)
            arr = src.read(
                1, window=window, out_shape=(tile_size, tile_size),
                resampling=Resampling.nearest)
    except Exception:
        return _empty_png(tile_size)

    img = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
    if kind == "change":
        for code in np.unique(arr):
            if code == 0 or code == NODATA:
                continue
            img[arr == code] = (*change_color_for_code(int(code)), 255)
    else:
        for cls, color in CLASS_COLORS.items():
            m = arr == cls
            if m.any():
                img[m] = (*color, 255)

    out = io.BytesIO()
    Image.fromarray(img, "RGBA").save(out, format="PNG")
    return out.getvalue()


def _empty_png(tile_size=256):
    import numpy as np
    from PIL import Image

    img = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
    out = io.BytesIO()
    Image.fromarray(img, "RGBA").save(out, format="PNG")
    return out.getvalue()


def write_geojson(change_code, transform, crs, path):
    """把变化栅格转矢量 GeoJSON，属性表带 from/to/transition/面积（UTM 米制）"""
    from rasterio.features import shapes as rio_shapes
    from shapely.geometry import shape

    features = []
    for geom, value in rio_shapes(change_code.astype("int16"), transform=transform):
        code = int(value)
        if code == 0:
            continue
        b, a = decode(code)
        try:
            area_m2 = float(shape(geom).area)
        except Exception:
            area_m2 = 0.0
        features.append({
            "type": "Feature",
            "properties": {
                "code": code,
                "from_class": CLASS_NAMES[b],
                "to_class": CLASS_NAMES[a],
                "transition": f"{CLASS_NAMES[b]} -> {CLASS_NAMES[a]}",
                "area_m2": round(area_m2, 1),
                "area_ha": round(area_m2 / 1e4, 4),
            },
            "geometry": geom,
        })

    # 兼容 rasterio CRS 对象与 "EPSG:xxxx" 字符串
    crs_name = crs.to_string() if hasattr(crs, "to_string") else str(crs)
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": crs_name}},
        "features": features,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, indent=1)
    return path


def process_change(bbox, year_before, year_after, out_dir, progress=None):
    """完整处理流程：拉取两个时相 -> 对齐 -> 统计 -> 输出 tif(含解码元数据) + geojson"""
    import numpy as np
    from pystac_client import Client

    _ensure_proj_env()

    def report(p, m):
        if progress:
            progress.update(p, m)

    if year_before not in YEARS or year_after not in YEARS:
        raise ValueError(f"年份必须在 {YEARS[0]}~{YEARS[-1]} 之间")
    if len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
        raise ValueError("bbox 必须是 [west, south, east, north] 四个数值")

    report(3, "连接 Planetary Computer STAC ...")
    catalog = Client.open(STAC_URL)

    before_range = f"{year_before}-01-01/{year_before}-12-31"
    after_range = f"{year_after}-01-01/{year_after}-12-31"

    report(10, f"拉取时相1（{year_before} 年）...")
    before_arr, before_transform, before_crs, profile, before_item = fetch_lulc_array(
        catalog, bbox, before_range, progress=progress)
    report(35, f"使用影像 {before_item}")

    report(40, f"拉取时相2（{year_after} 年）...")
    after_arr, after_transform, after_crs, _, after_item = fetch_lulc_array(
        catalog, bbox, after_range, progress=progress)
    report(60, f"使用影像 {after_item}")

    report(65, "对齐两期影像网格 ...")
    after_aligned = align_to_reference(
        after_arr, after_transform, after_crs,
        before_arr, before_transform, before_crs,
    )

    report(75, "逐像素比较，统计类别转移 ...")
    change_mask, transitions = summarize_transitions(before_arr, after_aligned)
    change_code = np.where(
        change_mask,
        before_arr.astype(np.int16) * 10 + after_aligned.astype(np.int16) + 1,
        0,
    ).astype(np.int16)

    total_px = int(before_arr.size)
    changed_px = int(change_mask.sum())
    pixel_area = abs(before_transform.a) * abs(before_transform.e)

    report(85, "写出 GeoTIFF（含解码元数据）与 GeoJSON 矢量 ...")
    tif_name = "change_map.tif"
    geojson_name = "change_map.geojson"
    before_tif = "before.tif"
    after_tif = "after.tif"
    write_tif_with_metadata(change_code, profile, before_transform, before_crs,
                            os.path.join(out_dir, tif_name))
    write_geojson(change_code, before_transform, before_crs,
                  os.path.join(out_dir, geojson_name))
    # 两时相分类栅格（after 为对齐后版本，与变化栅格同网格），供前端瓦片叠加显示
    write_class_tif(before_arr, profile, before_transform, before_crs,
                    os.path.join(out_dir, before_tif))
    write_class_tif(after_aligned, profile, before_transform, before_crs,
                    os.path.join(out_dir, after_tif))

    top_transitions = []
    for (b, a), cnt in sorted(transitions.items(), key=lambda x: -x[1])[:15]:
        top_transitions.append({
            "code": encode(b, a),
            "from_class": CLASS_NAMES[b],
            "to_class": CLASS_NAMES[a],
            "transition": f"{CLASS_NAMES[b]} -> {CLASS_NAMES[a]}",
            "pixels": cnt,
            "area_m2": cnt * pixel_area,
            "area_ha": cnt * pixel_area / 1e4,
            "percent_of_change": cnt / changed_px if changed_px else 0,
        })

    report(100, "完成")

    return {
        "before_item": before_item,
        "after_item": after_item,
        "crs": str(before_crs),
        "width": before_arr.shape[1],
        "height": before_arr.shape[0],
        "pixel_area_m2": pixel_area,
        "total_pixels": total_px,
        "changed_pixels": changed_px,
        "change_percent": changed_px / total_px if total_px else 0,
        "top_transitions": top_transitions,
        "files": {
            "tif": tif_name,
            "geojson": geojson_name,
            "before_tif": before_tif,
            "after_tif": after_tif,
        },
    }

