#!/usr/bin/env python3
"""
change_map.tif 的分析工具
=========================
对 change_detection_demo.py 输出的变化编码栅格做：
  1. 解码变化编码（0=无变化；否则 before=(code-1)//10, after=(code-1)%10）
  2. 变化面积统计（10m 分辨率，1 像素 = 100 ㎡）
  3. 类别转移矩阵 + 各类别净变化
  4. 可视化输出（转移矩阵热力图 + 变化分布图，保存为 change_analysis.png）

用法：
    python analyze_change_map.py [change_map.tif]
"""
import os
import sys
from collections import Counter

import numpy as np
import rasterio

# io-lulc-9-class 真实类别编码（没有 3 和 6；10=云、11=牧场；nodata=0）
CLASS_NAMES = {
    1: "水体", 2: "林地", 4: "洪泛植被", 5: "作物",
    7: "建设用地", 8: "裸地", 9: "雪/冰", 10: "云", 11: "牧场",
}
CLASS_LIST = [CLASS_NAMES[i] for i in CLASS_NAMES]


def decode(code):
    """变化编码 -> (before, after)，0 表示无变化返回 None（code = before*20 + after）"""
    if code == 0:
        return None
    return code // 20, code % 20


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "change_map.tif"
    with rasterio.open(path) as ds:
        code = ds.read(1)
        transform = ds.transform
        crs = ds.crs

    # 10m 分辨率，像素面积 = |a| * |e|
    pixel_area = abs(transform.a) * abs(transform.e)
    total_px = code.size

    changed = code != 0
    n_px = int(changed.sum())

    print(f"栅格尺寸       : {code.shape[1]} x {code.shape[0]} 像素 | CRS: {crs}")
    print(f"像素面积       : {pixel_area:.0f} 平方米 ({np.sqrt(pixel_area):.1f}m 分辨率)")
    print(f"研究区总面积   : {total_px * pixel_area / 1e6:.3f} km2")
    print(f"发生变化像素   : {n_px} ({n_px / total_px:.2%})")
    print(f"变化面积       : {n_px * pixel_area / 1e4:.1f} 公顷 = {n_px * pixel_area / 1e6:.4f} km2")

    # ---- 逐像素转移统计 ----
    rows, cols = np.nonzero(changed)
    counts = Counter(decode(int(code[r, c])) for r, c in zip(rows, cols))

    print("\n=== Top 15 变化类型（按面积） ===")
    print(f"{'变化类型':<22}{'像素':>8}{'面积(m2)':>11}{'面积(ha)':>10}{'占变化%':>9}")
    for (b, a), cnt in counts.most_common(15):
        area = cnt * pixel_area
        print(f"{CLASS_NAMES[b]}→{CLASS_NAMES[a]:<18}"
              f"{cnt:>8}{area:>11,.0f}{area / 1e4:>10.2f}{cnt / n_px:>9.2%}")

    # ---- 转移矩阵（行=前一时相，列=后一时相） ----
    mat = np.zeros((9, 9), dtype=np.int64)
    for (b, a), cnt in counts.items():
        mat[b - 1, a - 1] = cnt

    print("\n=== 转移矩阵（行=2021年 before，列=2022年 after） ===")
    print("        " + "".join(f"{n:>10}" for n in CLASS_LIST))
    for i, n in enumerate(CLASS_LIST):
        print(f"{n:>8}" + "".join(f"{v:>10}" for v in mat[i]))

    # ---- 净变化（正=增加，负=减少） ----
    net = mat - mat.T
    print("\n=== 各类别净变化（像素 / 公顷） ===")
    for i, n in enumerate(CLASS_LIST):
        d = int(net[i].sum())
        print(f"  {n}: {d:+d} 像素 ({d * pixel_area / 1e4:+.1f} 公顷)")

    # ---- 可视化 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import BoundaryNorm, ListedColormap
        # Windows 中文字体（按顺序回退）
        matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
    except ImportError:
        print("\n[提示] 未安装 matplotlib，跳过绘图。运行 pip install matplotlib 后可生成图表。")
        return

    uniq = np.unique(code)
    uniq = uniq[uniq != 0]

    fig, axes = plt.subplots(1, 2, figsize=(17, 7))

    # 左图：转移矩阵热力图
    im = axes[0].imshow(mat, cmap="YlOrRd", aspect="auto")
    axes[0].set_xticks(range(9)); axes[0].set_xticklabels(CLASS_LIST, rotation=45, ha="right")
    axes[0].set_yticks(range(9)); axes[0].set_yticklabels(CLASS_LIST)
    axes[0].set_xlabel("2022 年（变化后）")
    axes[0].set_ylabel("2021 年（变化前）")
    axes[0].set_title("类别转移矩阵（像素数）")
    for i in range(9):
        for j in range(9):
            if mat[i, j] > 0:
                axes[0].text(j, i, f"{mat[i, j]:,}", ha="center", va="center",
                             color="white" if mat[i, j] > mat.max() * 0.6 else "black",
                             fontsize=8)
    fig.colorbar(im, ax=axes[0], fraction=0.046, pad=0.04, label="像素数")

    # 右图：变化分布图（0 无变化=白，其余每种转移一个颜色）
    bounds = np.concatenate(([0], uniq, [uniq.max() + 1]))
    cmap = ListedColormap(["white"] + [plt.cm.tab20(i % 20) for i in range(len(uniq))])
    norm = BoundaryNorm(bounds, cmap.N)
    axes[1].imshow(code, cmap=cmap, norm=norm, interpolation="nearest")
    axes[1].set_title("变化空间分布（颜色=转移类型）")
    axes[1].set_xticks([]); axes[1].set_yticks([])

    labels = ["无变化"] + [
        f"{code} {CLASS_NAMES[code // 20]}→{CLASS_NAMES[code % 20]}"
        for code in uniq
    ]
    cbar = fig.colorbar(axes[1].images[0], ax=axes[1], fraction=0.046, pad=0.04, ticks=bounds[:-1])
    cbar.ax.set_yticklabels(labels, fontsize=7)
    cbar.ax.tick_params(length=0)

    out_png = os.path.splitext(path)[0] + "_analysis.png"
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"\n分析图已保存: {out_png}")


if __name__ == "__main__":
    main()
