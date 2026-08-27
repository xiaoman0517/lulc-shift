#!/usr/bin/env python3
"""本地 API 端到端测试：创建任务 -> 轮询进度 -> 下载结果"""
import json
import time

import requests

BASE = "http://127.0.0.1:8000"


def main():
    # 1. 基础端点
    r = requests.get(f"{BASE}/", timeout=30)
    print(f"GET / -> {r.status_code}, html={len(r.text)} bytes")
    r = requests.get(f"{BASE}/static/app.js", timeout=30)
    print(f"GET /static/app.js -> {r.status_code}, js={len(r.text)} bytes")
    r = requests.get(f"{BASE}/api/meta", timeout=30)
    meta = r.json()
    print(f"GET /api/meta -> years={meta['years']}")

    # 2. 创建任务（深圳南山小区域）
    payload = {
        "bbox": [113.90, 22.50, 114.00, 22.58],
        "year_before": 2021,
        "year_after": 2022,
    }
    r = requests.post(f"{BASE}/api/jobs", json=payload, timeout=30)
    print(f"POST /api/jobs -> {r.status_code}, {r.json()}")
    r.raise_for_status()
    job_id = r.json()["job_id"]

    # 3. 轮询
    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(2)
        r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=30)
        job = r.json()
        pct = job["percent"]
        last_log = job["logs"][-1]["message"] if job["logs"] else ""
        print(f"  status={job['status']:>8} percent={pct:>3}%  {last_log}")
        if job["status"] == "done":
            break
        if job["status"] == "error":
            print("JOB ERROR:", job["error"])
            raise SystemExit(1)
    else:
        print("超时未完成")
        raise SystemExit(1)

    result = job["result"]
    print(f"\n结果: {result['before_item']} -> {result['after_item']}")
    print(f"  变化像素: {result['changed_pixels']} ({result['change_percent']*100:.2f}%)")
    print("  Top5 转移:")
    for t in result["top_transitions"][:5]:
        print(f"    {t['transition']}: {t['pixels']} px ({t['area_ha']:.2f} ha)")

    # 4. 下载
    for fmt in ("tif", "geojson"):
        r = requests.get(f"{BASE}/api/jobs/{job_id}/download", params={"fmt": fmt}, timeout=60)
        print(f"download {fmt}: {r.status_code}, {len(r.content)} bytes")
        r.raise_for_status()
        with open(f"test_output.{'tif' if fmt == 'tif' else 'geojson'}", "wb") as f:
            f.write(r.content)

    print("\nAPI 端到端测试通过")


if __name__ == "__main__":
    main()
