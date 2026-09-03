#!/usr/bin/env python3
"""
Land Cover Change Detection - Web API
=====================================
FastAPI 应用（根目录入口，供 Vercel Python zero-config 检测）：
  - GET  /                   前端页面（静态托管）
  - GET  /static/{name}      前端静态资源
  - GET  /api/meta           数据源/年份/类别元信息
  - GET  /api/diag           运行时诊断（引擎加载 / 存储后端状态）
  - POST /api/jobs           创建变化检测任务 {bbox, year_before, year_after}
  - GET  /api/jobs/{id}      轮询任务状态与进度（Serverless 下会自动"接管"执行）
  - GET  /api/jobs/{id}/download?fmt=tif|geojson   下载结果
  - GET  /api/jobs/{id}/download/zip               打包下载全部结果

本地运行：
    pip install -r requirements.txt
    uvicorn app:app --reload

Vercel 部署：
    入口文件必须是根目录的 app.py（Vercel 自动识别 FastAPI 并"按原始路径"
    把每个请求路由到本应用）。不要在 vercel.json 里添加 rewrites 到 api/index.py
    ——Vercel 的 FastAPI 路由会把重写后的路径透传给应用，导致 404 {"detail":"Not Found"}。

存储架构（解决 Serverless 无状态问题）：
    Vercel 函数是无状态、按请求隔离、实例短暂的环境。本地常驻进程里依赖的
    "内存任务表 + 后台线程 + /tmp 本地文件"三者会在部署后全部失效，表现为
    下载提示"无法从网站上获取文件"、地图瓦片加载不出来。因此本项目提供两种
    存储模式，按环境变量自动切换：

      1) Serverless 模式（部署到 Vercel 时，推荐）：
         - 任务状态  -> Redis，兼容两套环境变量（同构，任选其一即可）：
             a) Upstash 原生 UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN
             b) Vercel KV（Vercel Storage -> KV 集成自动注入）KV_REST_API_URL / KV_REST_API_TOKEN
         - 结果文件  -> Vercel Blob（环境变量 BLOB_READ_WRITE_TOKEN，Vercel Storage -> Blob 自动注入）
         - 不启动后台线程（Vercel 会在响应返回后冻结实例）；GET /api/jobs/{id}
           轮询时若发现任务长时间没有进度更新，就在该请求内"接管"继续执行，
           直到完成或被函数时长上限打断（下次轮询会再次接管；重复执行幂等，
           最终收敛到 done）。

      2) 本地/回退模式（未配置上述环境变量时）：
         保持原有"进程内存任务表 + 后台线程 + /tmp 文件"，本地 uvicorn 体验不变。

    /api/diag 会返回当前实际使用的存储后端，方便排查。
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import uuid
import zipfile

# 确保能 import 项目根目录的 engine.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, JSONResponse
from pydantic import BaseModel

# ---- 预置 PROJ 数据库路径（在导入 rasterio 之前执行，避免被系统 PROJ_LIB 污染）----
# 用 find_spec 定位但不导入 rasterio，保证函数启动保持轻量
try:
    import importlib.util
    _rasterio_spec = importlib.util.find_spec("rasterio")
    if _rasterio_spec and _rasterio_spec.origin:
        _proj_dir = os.path.join(os.path.dirname(_rasterio_spec.origin), "proj_data")
        if os.path.isdir(_proj_dir):
            os.environ["PROJ_DATA"] = _proj_dir
            os.environ["PROJ_LIB"] = _proj_dir
except Exception:  # noqa: BLE001
    pass

# ---- 懒加载 engine：模块加载阶段绝不导入重型科学计算库（避免函数启动崩溃）----
_engine = None
_engine_error = None


def get_engine():
    """首次调用时才导入 engine；失败时抛出带细节的 HTTP 错误"""
    global _engine, _engine_error
    if _engine is None and _engine_error is None:
        try:
            import importlib
            _engine = importlib.import_module("engine")
        except Exception as exc:  # noqa: BLE001
            _engine_error = f"{type(exc).__name__}: {exc}"
    if _engine is None:
        raise HTTPException(status_code=500, detail=f"处理引擎加载失败：{_engine_error}")
    return _engine

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WORK_ROOT = os.path.join(tempfile.gettempdir(), "lulc_jobs")

app = FastAPI(title="Land Cover Change Detection", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 任务状态存储 ----
# 内存任务表：本地/回退模式的兜底存储（Serverless 模式使用 Upstash Redis，见下）
JOBS = {}

# Serverless 模式下任务记录在 Redis 中的保留时长（24h），超时自动过期释放空间
JOB_TTL = 60 * 60 * 24
# running 任务超过 N 秒没有进度更新，视为执行者失联，下一次轮询请求将"接管"执行
STALE_AFTER = 30

_redis_client = None
_redis_error = None
_use_redis = False

# Redis REST 环境变量两套命名等价（底层都是 Upstash Redis）：
#   a) Upstash 集成/手动配置：UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN
#   b) Vercel KV（Vercel Storage -> KV 自动注入）：KV_REST_API_URL / KV_REST_API_TOKEN
_REDIS_URL_ENVS = ("UPSTASH_REDIS_REST_URL", "KV_REST_API_URL")
_REDIS_TOKEN_ENVS = ("UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN")


def get_redis():
    """懒加载 Redis 客户端；只要任一命名下的 URL+Token 都配置了就启用 Redis 模式。"""
    global _redis_client, _redis_error, _use_redis
    if _redis_client is None and _redis_error is None:
        url = next((os.environ.get(k, "").strip() for k in _REDIS_URL_ENVS if os.environ.get(k, "").strip()), "")
        token = next((os.environ.get(k, "").strip() for k in _REDIS_TOKEN_ENVS if os.environ.get(k, "").strip()), "")
        if url and token:
            try:
                from upstash_redis import Redis
                _redis_client = Redis(url=url, token=token, allow_telemetry=False)
                _use_redis = True
            except Exception as exc:  # noqa: BLE001
                _redis_error = f"{type(exc).__name__}: {exc}"
        else:
            _redis_error = (
                "未检测到 Redis 环境变量（需同时配置 URL+Token）："
                f"{_REDIS_URL_ENVS[0]}/{_REDIS_TOKEN_ENVS[0]} 或 Vercel KV 的 "
                f"{_REDIS_URL_ENVS[1]}/{_REDIS_TOKEN_ENVS[1]}"
            )
    return _redis_client if _use_redis else None


def _job_key(job_id):
    return f"lulc:job:{job_id}"


def _save_job(job):
    """保存任务状态。Redis 模式下尽力而为，写入失败返回 False；本地模式写内存表。"""
    r = get_redis()
    if r:
        try:
            r.set(_job_key(job["job_id"]), json.dumps(job, ensure_ascii=False), ex=JOB_TTL)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[storage] Redis 写入失败: {exc}", file=sys.stderr)
            return False
    JOBS[job["job_id"]] = job
    return True


def _load_job(job_id):
    """读取任务状态（Redis 优先，回退内存表）。"""
    r = get_redis()
    if r:
        try:
            raw = r.get(_job_key(job_id))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[storage] Redis 读取失败: {exc}", file=sys.stderr)
            return None
    return JOBS.get(job_id)


def _blob_enabled():
    """是否启用 Vercel Blob 存储（结果文件上传到对象存储，跨实例可下载）。"""
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip())


def _download_url(url):
    """给 Blob 公开 URL 追加 ?download=1，强制浏览器以附件方式下载。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}download=1"


_ZIP_README = (
    "土地覆盖变化检测结果\n"
    "====================\n"
    "变化编码规则：0=无变化；code = before*20 + after，即 before = code//20，after = code%20\n"
    "类别编码：1=水体 2=林地 4=洪泛植被 5=作物 7=建设用地 8=裸地 9=雪/冰 10=云 11=牧场；nodata=0\n\n"
    "文件说明：\n"
    "  before.tif        变化前分类栅格（10m）\n"
    "  after.tif         变化后分类栅格（10m）\n"
    "  change_map.tif    变化编码栅格（CHANGE_DECODE 元数据标签含完整解码表）\n"
    "  change_map.geojson 变化多边形矢量（属性表含 code/from_class/to_class/transition/area）\n"
    "  可在 QGIS / ArcGIS 中直接打开。\n"
)


def _build_zip_bytes(local_files):
    """把四个本地结果文件打包成 zip 字节流（含 README.txt）。"""
    members = [
        ("before", "before.tif"),
        ("after", "after.tif"),
        ("tif", "change_map.tif"),
        ("geojson", "change_map.geojson"),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, name in members:
            path = local_files.get(key)
            if path and os.path.exists(path):
                zf.write(path, name)
        zf.writestr("README.txt", _ZIP_README)
    buffer.seek(0)
    return buffer.getvalue()


def _upload_results(job_id, local_files):
    """把本地结果文件上传到 Vercel Blob（公开读），返回 {key: blob url}，并额外上传打包 zip。"""
    import vercel_blob

    def put_one(pathname, data, multipart):
        resp = vercel_blob.put(
            pathname,
            data,
            {"addRandomSuffix": True, "allowOverwrite": True},
            multipart=multipart,
            timeout=300,  # 大文件上传需要更长超时（默认 10s 不够）
        )
        return resp.get("url") or resp.get("downloadUrl") or ""

    urls = {}
    for key, path in local_files.items():
        with open(path, "rb") as f:
            data = f.read()
        pathname = f"{job_id}/{os.path.basename(path)}"
        urls[key] = put_one(pathname, data, len(data) > 8 * 1024 * 1024)
    zip_bytes = _build_zip_bytes(local_files)
    urls["zip"] = put_one(
        f"{job_id}/land_cover_change.zip", zip_bytes, len(zip_bytes) > 8 * 1024 * 1024
    )
    return urls


# 瓦片渲染用的 Blob tif -> 本地临时文件缓存（按实例内存缓存 + /tmp 落盘）
_TIF_FILE_CACHE = {}


def _tif_local_path(url):
    """把 blob 上的 tif 拉取到本地临时文件（实例内缓存），供瓦片渲染使用。"""
    cached = _TIF_FILE_CACHE.get(url)
    if cached and os.path.exists(cached):
        return cached
    cache_dir = os.path.join(tempfile.gettempdir(), "lulc_tif_cache")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, hashlib.md5(url.encode("utf-8")).hexdigest() + ".tif")
    try:
        import requests as _requests
        data = _requests.get(url, timeout=120).content
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    _TIF_FILE_CACHE[url] = path
    if len(_TIF_FILE_CACHE) > 24:
        _TIF_FILE_CACHE.clear()
    return path


def _cleanup_local(local_files, workdir):
    """Blob 模式上传完成后删除本地临时产物，避免占满 Serverless 的 /tmp 配额。"""
    for path in local_files.values():
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(workdir)
    except OSError:
        pass


class JobRequest(BaseModel):
    bbox: list[float]
    year_before: int
    year_after: int


def _job_from_progress(job, progress):
    job["percent"] = progress.percent
    job["logs"] = list(progress.logs)
    job["updated_at"] = int(time.time())
    _save_job(job)


def _run_job(job_id):
    """执行一个变化检测任务：拉取 -> 对齐 -> 统计 -> 输出文件 -> 上传 Blob/落盘。

    - 本地模式：由创建任务时启动的后台线程调用（进程内存任务表，与旧逻辑一致）；
    - Serverless 模式：由轮询请求在"接管"时内联调用（状态持久化到 Redis），
      若单次请求被函数时长上限打断，下一次轮询会再次接管重跑（幂等，最终收敛）。
    """
    job = _load_job(job_id)
    if not job or job.get("status") in ("done", "error"):
        return
    workdir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(workdir, exist_ok=True)
    try:
        eng = get_engine()
        progress = eng.Progress(sink=lambda p: _job_from_progress(job, p))
        result = eng.process_change(
            job["bbox"], job["year_before"], job["year_after"], workdir, progress=progress
        )
        job["status"] = "done"
        job["percent"] = 100
        job["result"] = result
        local_files = {
            "tif": os.path.join(workdir, result["files"]["tif"]),
            "geojson": os.path.join(workdir, result["files"]["geojson"]),
            "before": os.path.join(workdir, result["files"]["before_tif"]),
            "after": os.path.join(workdir, result["files"]["after_tif"]),
        }
        if job.get("storage") == "blob":
            urls = _upload_results(job_id, local_files)
            urls["change"] = urls["tif"]  # 瓦片 layer=change 复用变化栅格
            job["files"] = urls
            _cleanup_local(local_files, workdir)
        else:
            job["files"] = dict(local_files)
            job["files"]["change"] = local_files["tif"]
        job["updated_at"] = int(time.time())
        _save_job(job)
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["updated_at"] = int(time.time())
        _save_job(job)


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


@app.get("/api/diag")
def diag():
    """诊断端点：不加载任何重型依赖，用于排查线上函数启动/存储配置问题"""
    get_redis()  # 触发一次 Redis 初始化探测
    if _use_redis and _blob_enabled():
        storage_mode = "redis+blob"
    elif _use_redis:
        storage_mode = "redis-only"
    elif _blob_enabled():
        storage_mode = "blob-only"
    else:
        storage_mode = "local-memory"
    return {
        "ok": _engine is not None,
        "engine_error": _engine_error,
        "note": "重型依赖（numpy/rasterio 等）在首次任务请求时才加载，本端点不加载它们",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "temp": tempfile.gettempdir(),
        "storage": {
            "mode": storage_mode,
            "redis": bool(_use_redis),
            "blob": bool(_blob_enabled()),
            "env_set": sorted(
                k for k in (
                    "UPSTASH_REDIS_REST_URL",
                    "UPSTASH_REDIS_REST_TOKEN",
                    "KV_REST_API_URL",
                    "KV_REST_API_TOKEN",
                    "BLOB_READ_WRITE_TOKEN",
                ) if os.environ.get(k)
            ),
        },
    }


@app.get("/api/meta")
def meta():
    eng = get_engine()
    return {
        "collection": eng.COLLECTION,
        "stac_url": eng.STAC_URL,
        "years": eng.YEARS,
        "class_names": eng.CLASS_NAMES,
        "nodata": eng.NODATA,
    }


@app.post("/api/jobs")
def create_job(req: JobRequest):
    eng = get_engine()
    if req.year_before not in eng.YEARS or req.year_after not in eng.YEARS:
        raise HTTPException(status_code=400, detail=f"年份必须在 {eng.YEARS} 内")
    if len(req.bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox 必须是 [west, south, east, north]")
    w, s, e, n = req.bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(status_code=400, detail="bbox 范围非法")
    if e - w > 0.5 or n - s > 0.5:
        raise HTTPException(
            status_code=400,
            detail="BBOX 过大（单边最大 0.5°，约 55km），请缩小监测范围",
        )

    job_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    job = {
        "job_id": job_id,
        "status": "running",
        "percent": 0,
        "logs": [],
        "error": None,
        "result": None,
        "files": {},
        "storage": "blob" if _blob_enabled() else "local",
        "bbox": req.bbox,
        "year_before": req.year_before,
        "year_after": req.year_after,
        "created_at": now,
        # updated_at=0 表示"尚无执行者"：Serverless 模式下，第一个轮询请求会立即接管执行
        "updated_at": 0,
    }
    if not _save_job(job):
        raise HTTPException(
            status_code=500,
            detail="任务状态写入失败：请检查 Redis 环境变量是否配置正确"
            "(UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN 或 Vercel KV 的 KV_REST_API_URL/KV_REST_API_TOKEN)",
        )
    if not _use_redis:
        # 本地模式：后台线程执行，保持原有"创建 -> 轮询"体验
        threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    # Serverless 模式不启动后台线程：Vercel 会在响应返回后冻结实例，
    # 线程不可靠。由 GET /api/jobs/{id} 轮询时检测到 updated_at=0/失联后接管执行。
    return {"job_id": job_id, "status": "running"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在（任务记录已过期或从未创建）")

    # Serverless 接管：任务仍 running 且长时间没有进度更新（执行者被冻结/回收/超时打断）
    # 时，由本请求内联继续执行。执行期间会持续刷新 updated_at，其他并发轮询看到
    # 新鲜进度就不会重复接管；重复执行幂等，最终收敛到 done。
    if (
        _use_redis
        and job["status"] == "running"
        and int(time.time()) - (job.get("updated_at") or 0) >= STALE_AFTER
    ):
        _run_job(job_id)
        job = _load_job(job_id) or job

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
    job = _load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    if fmt not in job["files"]:
        raise HTTPException(status_code=400, detail="fmt 必须为 tif 或 geojson")

    # Blob 模式：302 跳转到对象存储的公开 URL（?download=1 强制下载），
    # 不占函数带宽，且与执行任务的是哪个实例无关
    if job.get("storage") == "blob":
        url = job["files"].get(fmt)
        if not url:
            raise HTTPException(status_code=404, detail="结果文件不存在")
        return RedirectResponse(_download_url(url))

    # 本地模式：直接从磁盘文件返回
    path = job["files"][fmt]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="结果文件不存在")
    media = "image/tiff" if fmt == "tif" else "application/geo+json"
    return FileResponse(path, media_type=media, filename=os.path.basename(path))


@app.get("/api/jobs/{job_id}/download/zip")
def download_zip(job_id: str):
    """一次性打包下载全部结果：前后分类 + 变化栅格 + 带属性的矢量变化图层"""
    job = _load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")

    if job.get("storage") == "blob":
        zip_url = job["files"].get("zip")
        if zip_url:
            return RedirectResponse(_download_url(zip_url))
        # 兜底：zip 未成功上传时，从各 Blob 拉取原始文件现场打包
        import requests as _requests
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for key, name in (
                ("before", "before.tif"),
                ("after", "after.tif"),
                ("tif", "change_map.tif"),
                ("geojson", "change_map.geojson"),
            ):
                url = job["files"].get(key)
                if url:
                    zf.writestr(name, _requests.get(url, timeout=120).content)
            zf.writestr("README.txt", _ZIP_README)
        buffer.seek(0)
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="land_cover_change.zip"'},
        )

    # 本地模式：直接从磁盘文件打包
    members = [
        ("before", "before.tif", "变化前分类栅格"),
        ("after", "after.tif", "变化后分类栅格"),
        ("tif", "change_map.tif", "变化编码栅格"),
        ("geojson", "change_map.geojson", "变化矢量（含属性）"),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, name, desc in members:
            path = job["files"].get(key)
            if path and os.path.exists(path):
                zf.write(path, name)
        zf.writestr("README.txt", _ZIP_README)
    buffer.seek(0)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="land_cover_change.zip"'},
    )


# 瓦片渲染内存缓存（demo 级；key 含 tif 路径，任务完成后结果不可变，缓存安全）
_TILE_CACHE = {}
_TILE_CACHE_MAX = 400


@app.get("/api/jobs/{job_id}/tiles/{layer}/{z}/{x}/{y}.png")
def tile(job_id: str, layer: str, z: int, x: int, y: int):
    if layer not in ("before", "after", "change"):
        raise HTTPException(status_code=400, detail="layer 必须为 before/after/change")
    job = _load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")

    if job.get("storage") == "blob":
        url = job["files"].get(layer)
        if not url:
            raise HTTPException(status_code=404, detail="图层文件不存在")
        try:
            tif_path = _tif_local_path(url)  # 从 Blob 拉取到本实例 /tmp（带缓存）
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"拉取图层文件失败：{exc}") from exc
    else:
        tif_path = job["files"].get(layer)
        if not tif_path or not os.path.exists(tif_path):
            raise HTTPException(status_code=404, detail="图层文件不存在")

    key = (job_id, layer, z, x, y)
    png = _TILE_CACHE.get(key)
    if png is None:
        png = get_engine().render_tile(tif_path, z, x, y, kind="change" if layer == "change" else "class")
        if len(_TILE_CACHE) >= _TILE_CACHE_MAX:
            _TILE_CACHE.clear()
        _TILE_CACHE[key] = png
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


