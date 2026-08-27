#!/usr/bin/env python3
"""
Land Cover Change Detection - Web API
=====================================
FastAPI 应用，提供：
  - GET  /                   前端页面（静态托管）
  - GET  /static/{name}      前端静态资源
  - GET  /api/meta           数据源/年份/类别元信息
  - POST /api/jobs           创建变化检测任务 {bbox, year_before, year_after}
  - GET  /api/jobs/{id}      轮询任务状态与进度
  - GET  /api/jobs/{id}/download?fmt=tif|geojson   下载结果

本地运行：
    pip install -r requirements.txt
    uvicorn api.index:app --reload

Vercel 部署：
    vercel.json 已配置 Python runtime + rewrites，直接 `vercel --prod`。
注意：任务状态保存在进程内存中（demo 级别）。多实例/冷启动时进度可能丢失，
生产环境应替换为 Redis/Postgres 等持久化队列，见 README。
"""
import os
import sys
import tempfile
import threading
import uuid

# 确保能 import 项目根目录的 engine.py（Vercel / 本地 uvicorn 通用）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

import engine

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
WORK_ROOT = os.path.join(tempfile.gettempdir(), "lulc_jobs")

app = FastAPI(title="Land Cover Change Detection", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 内存任务表：job_id -> {"status","percent","logs","error","result","files"}
JOBS = {}


class JobRequest(BaseModel):
    bbox: list[float]
    year_before: int
    year_after: int


def _job_from_progress(job, progress):
    job["percent"] = progress.percent
    job["logs"] = list(progress.logs)


def _run_job(job_id, req):
    job = JOBS[job_id]
    workdir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(workdir, exist_ok=True)
    try:
        progress = engine.Progress(sink=lambda p: _job_from_progress(job, p))
        result = engine.process_change(
            req.bbox, req.year_before, req.year_after, workdir, progress=progress
        )
        job["status"] = "done"
        job["percent"] = 100
        job["result"] = result
        job["files"] = {
            "tif": os.path.join(workdir, result["files"]["tif"]),
            "geojson": os.path.join(workdir, result["files"]["geojson"]),
            "before": os.path.join(workdir, result["files"]["before_tif"]),
            "after": os.path.join(workdir, result["files"]["after_tif"]),
            "change": os.path.join(workdir, result["files"]["tif"]),
        }
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)


@app.get("/")
def index():
    path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(path):
        return HTMLResponse("前端文件缺失（static/index.html）", status_code=500)
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/static/{name}")
def static_file(name: str):
    # 只允许静态目录内的文件，防止路径穿越
    full = os.path.realpath(os.path.join(STATIC_DIR, os.path.basename(name)))
    if not full.startswith(os.path.realpath(STATIC_DIR)) or not os.path.exists(full):
        raise HTTPException(status_code=404)
    return FileResponse(full)


@app.get("/api/meta")
def meta():
    return {
        "collection": engine.COLLECTION,
        "stac_url": engine.STAC_URL,
        "years": engine.YEARS,
        "class_names": engine.CLASS_NAMES,
        "nodata": engine.NODATA,
    }


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if req.year_before not in engine.YEARS or req.year_after not in engine.YEARS:
        raise HTTPException(status_code=400, detail=f"年份必须在 {engine.YEARS} 内")
    if len(req.bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox 必须是 [west, south, east, north]")
    w, s, e, n = req.bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(status_code=400, detail="bbox 范围非法")
    if e - w > 5 or n - s > 5:
        raise HTTPException(
            status_code=400,
            detail="BBOX 过大（最大 5°×5°），过大的区域会超出单瓦片范围并导致处理时间过长",
        )

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "percent": 0,
        "logs": [],
        "error": None,
        "result": None,
        "files": {},
    }
    threading.Thread(target=_run_job, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启后任务会丢失）")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "percent": job["percent"],
        "logs": job["logs"],
        "error": job["error"],
        "result": job["result"],
    }


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, fmt: str = "tif"):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    if fmt not in job["files"]:
        raise HTTPException(status_code=400, detail="fmt 必须为 tif 或 geojson")
    path = job["files"][fmt]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="结果文件不存在")
    media = "image/tiff" if fmt == "tif" else "application/geo+json"
    return FileResponse(path, media_type=media, filename=os.path.basename(path))


# 瓦片渲染内存缓存（demo 级；key 含 tif 路径，任务完成后结果不可变，缓存安全）
_TILE_CACHE = {}
_TILE_CACHE_MAX = 400


@app.get("/api/jobs/{job_id}/tiles/{layer}/{z}/{x}/{y}.png")
def tile(job_id: str, layer: str, z: int, x: int, y: int):
    if layer not in ("before", "after", "change"):
        raise HTTPException(status_code=400, detail="layer 必须为 before/after/change")
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    path = job["files"].get(layer)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="图层文件不存在")

    key = (job_id, layer, z, x, y)
    png = _TILE_CACHE.get(key)
    if png is None:
        png = engine.render_tile(path, z, x, y, kind="change" if layer == "change" else "class")
        if len(_TILE_CACHE) >= _TILE_CACHE_MAX:
            _TILE_CACHE.clear()
        _TILE_CACHE[key] = png
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )
