#!/usr/bin/env python3
"""验证瓦片渲染端点与前端资源（临时测试脚本）"""
import io
import math
import time

import requests
from PIL import Image

BASE = "http://127.0.0.1:8000"


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    xt = (lon + 180) / 360.0 * n
    latr = math.radians(lat)
    yt = (1 - math.asinh(math.tan(latr)) / math.pi) / 2.0 * n
    return int(xt), int(yt)


def main():
    # 页面与静态资源
    r = requests.get(f"{BASE}/", timeout=30)
    assert r.status_code == 200 and 'id="map"' in r.text, "首页异常"
    r = requests.get(f"{BASE}/static/app.js", timeout=30)
    assert r.status_code == 200 and "卷帘" in r.text, "app.js 异常"
    print("页面与静态资源 OK")

    # 创建任务
    payload = {"bbox": [113.90, 22.50, 114.00, 22.58], "year_before": 2021, "year_after": 2022}
    r = requests.post(f"{BASE}/api/jobs", json=payload, timeout=30)
    job_id = r.json()["job_id"]
    print(f"job_id={job_id}")

    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(3)
        job = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=30).json()
        print(f"  status={job['status']} percent={job['percent']}")
        if job["status"] == "done":
            break
        if job["status"] == "error":
            raise SystemExit(f"JOB ERROR: {job['error']}")
    else:
        raise SystemExit("超时")

    # 瓦片测试（center tiles 及相邻）
    for z in (10, 11, 12, 13):
        xt, yt = lonlat_to_tile(113.95, 22.54, z)
        for dx, dy in ((0, 0), (1, 0), (0, 1)):
            for layer in ("before", "after", "change"):
                url = f"{BASE}/api/jobs/{job_id}/tiles/{layer}/{z}/{xt+dx}/{yt+dy}.png"
                r = requests.get(url, timeout=60)
                assert r.status_code == 200 and r.headers["content-type"] == "image/png", (url, r.status_code)
                img = Image.open(io.BytesIO(r.content))
                assert img.size == (256, 256), (url, img.size)
            print(f"  z={z} tile ({xt},{yt}) 三层 OK")

    # 越界 tile 返回透明 PNG
    r = requests.get(f"{BASE}/api/jobs/{job_id}/tiles/before/10/0/0.png", timeout=60)
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (256, 256)
    print("越界 tile OK")

    # 下载 geojson 预览
    r = requests.get(f"{BASE}/api/jobs/{job_id}/download", params={"fmt": "geojson"}, timeout=60)
    assert r.status_code == 200 and len(r.content) > 10000
    print(f"geojson 下载 OK ({len(r.content)} bytes)")
    print("全部验证通过")


if __name__ == "__main__":
    main()
