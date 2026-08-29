#!/usr/bin/env python3
"""
基于 Microsoft Planetary Computer 的 io-lulc-9-class 数据集做"分类后比较"变化检测（CLI 版本）
思路：不训练任何模型，直接拉取两个年份的预训练分类结果，逐像素比较类别是否发生变化。

依赖：
    pip install -r requirements.txt

用法：
    1. 改下面 CONFIG 里的 BBOX（你要监测的区域，WGS84经纬度）和两个年份
    2. python change_detection_demo.py
    3. 输出 change_map.tif（变化编码栅格，含 CHANGE_DECODE 解码元数据）
       与 change_map.geojson（变化矢量，属性表带转换信息）

说明：
    - io-lulc-9-class 是 Impact Observatory 提供的10m全球年度土地覆盖分类产品，
      托管在 Planetary Computer，STAC API 免费公开访问
    - 该 collection 目前只覆盖 2017~2022 年，年份必须在此范围内
    - 类别编码（无 3 和 6）: 1=水体 2=林地 4=洪泛植被 5=作物 7=建设用地
      8=裸地 9=雪/冰 10=云 11=牧场；nodata 值为 0
    - 变化编码：0=无变化；code = before*20 + after
"""
import os
import shutil

import engine

CONFIG = {
    # 目标区域的经纬度范围 [west, south, east, north]，示例是深圳南山区一小块
    "BBOX": [113.90, 22.50, 114.00, 22.58],

    # 两个对比年份（必须在 2017~2022 内）
    "YEAR_BEFORE": 2021,
    "YEAR_AFTER": 2022,

    "OUTPUT_DIR": ".",  # 结果输出目录
}


def main():
    bbox = CONFIG["BBOX"]
    yb = CONFIG["YEAR_BEFORE"]
    ya = CONFIG["YEAR_AFTER"]
    out_dir = os.path.abspath(CONFIG["OUTPUT_DIR"])
    os.makedirs(out_dir, exist_ok=True)

    progress = engine.Progress()
    try:
        result = engine.process_change(bbox, yb, ya, out_dir, progress=progress)
    except Exception as exc:  # noqa: BLE001
        print(f"处理失败: {exc}")
        raise SystemExit(1)

    # 打印处理日志
    for log in progress.logs:
        print(f"  {log['at']}  {log['message']}")

    print(f"\n影像: {result['before_item']} -> {result['after_item']}")
    print(f"区域: {result['width']}x{result['height']} 像素，{result['pixel_area_m2']:.0f} m2/像素，CRS {result['crs']}")
    print(f"共 {result['changed_pixels']} 个像素发生类别变化（占比 {result['change_percent']:.2%}）")
    print(f"变化面积约 {result['changed_pixels'] * result['pixel_area_m2'] / 1e4:.1f} 公顷")

    print("\nTop 10 变化类型：")
    for t in result["top_transitions"][:10]:
        print(f"  code={t['code']:<4} {t['transition']}: {t['pixels']} 像素 ({t['area_ha']:.2f} ha)")

    # 结果文件已在 out_dir 中生成
    for key in ("tif", "geojson"):
        src = os.path.join(out_dir, result["files"][key])
        if CONFIG["OUTPUT_DIR"] == ".":
            dst = src
        else:
            dst = os.path.join(out_dir, result["files"][key])
            shutil.copy2(src, dst)
        print(f"\n{result['files'][key]} 已保存到 {dst}")

    print("\nGeoTIFF 已内置 CHANGE_DECODE 解码元数据；GeoJSON 属性表含转换信息，可直接在 QGIS 打开。")


if __name__ == "__main__":
    main()
