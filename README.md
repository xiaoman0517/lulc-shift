# 土地覆盖变化检测 · Land Cover Change Detection

基于 Microsoft Planetary Computer 的 [io-lulc-9-class](https://planetarycomputer.microsoft.com/dataset/io-lulc-9-class)
数据集（Impact Observatory 提供的 **10m 全球年度土地覆盖分类**，2017~2022），
做"分类后比较"（Post-Classification Comparison）变化检测的开源 Demo：

- 免训练、零成本：直接拉取两个年份的预训练分类影像，逐像素比较类别是否变化
- 输出 **变化编码栅格（GeoTIFF，自带解码元数据）** 与 **变化矢量（GeoJSON，属性表带转换信息）**
- 可一键部署到 **Vercel**，浏览器操作：框选范围 → 选年份 → 看进度 → 预览结果 → 下载

## 功能特性

- 🗺️ **地图框选监测范围**（Leaflet + Leaflet.Draw），也支持手动输入 BBOX
- 📅 **选择变化前 / 变化后年份**（2017~2022 任选）
- ⏳ **任务式处理**：提交后轮询进度 + 实时日志，完成后一键下载
- 🖼️ **前端直接预览结果**：
  - 三组瓦片图层（before / after / 变化栅格）叠加显示，右上角「图层」面板任意切换
  - **变化矢量** GeoJSON 直接渲染，点击多边形弹出属性
  - **卷帘对比**：点击地图左上角「⇔ 卷帘」按钮，左右拖动竖线直观对比前后时相
- 📊 **图例**：影像类别图例（Dynamic World 配色）+ 变化类型图例
- 🖥️ 同时提供 CLI 脚本（`change_detection_demo.py`）与分析工具（`analyze_change_map.py`）

## 结果文件

| 文件 | 说明 |
|------|------|
| `change_map.tif` | 变化编码栅格；完整解码表写入 GeoTIFF 元数据（命名空间 `CHANGE_DECODE`，73 个标签） |
| `change_map.geojson` | 变化多边形矢量；属性表含 `code / from_class / to_class / transition / area_m2 / area_ha` |
| `before.tif` / `after.tif` | 两时相分类栅格（供前端瓦片叠加 / 卷帘显示） |

## 编码规则

| 值 | 含义 |
|----|------|
| `0` | 无变化 |
| 其他 | `code = before × 20 + after`，即 `before = code // 20`，`after = code % 20` |

类别编码（无 3 和 6）：`1=水体 2=林地 4=洪泛植被 5=作物 7=建设用地 8=裸地 9=雪/冰 10=云 11=牧场`，nodata=`0`。

例如 `code=73` → `before=7`（建设用地）→ `after=2`（林地），即"建设用地 → 林地"。

## 目录结构

```
├── app.py                   # FastAPI 应用（Vercel Python 根目录入口，zero-config）
├── engine.py                 # 处理引擎：拉取/对齐/统计 + tif 元数据 + GeoJSON + 瓦片渲染
├── static/                   # 前端页面（index.html / app.js / style.css）
├── change_detection_demo.py  # CLI 版本
├── analyze_change_map.py     # 结果分析工具（转移矩阵 / 面积 / 可视化）
├── test_api.py               # API 端到端测试（任务/进度/下载）
├── test_frontend.py          # 瓦片渲染与前端资源测试
├── vercel.json               # Vercel 部署配置
├── requirements.txt
└── README.md
```

## 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2a. 命令行方式
python change_detection_demo.py

# 2b. 启动 Web Demo
uvicorn app:app --reload
# 浏览器打开 http://127.0.0.1:8000
```

> 注意：如果你的机器设置了系统代理（如 Clash，常见 `127.0.0.1:10809`），
> 运行前请设置 `NO_PROXY=*`（Windows PowerShell：`$env:NO_PROXY='*'; $env:no_proxy='*'`），
> 否则 Python 访问外网会报 SSL EOF。

## 部署到 GitHub + Vercel

### 1. 推送到 GitHub

```bash
git init
git add .
git commit -m "feat: land cover change detection demo (raster + vector + swipe preview)"
# 在 GitHub 创建仓库后：
git remote add origin https://github.com/<你的用户名>/land-cover-analysis.git
git push -u origin main
```

### 2. 导入到 Vercel

1. 打开 [vercel.com/new](https://vercel.com/new)，选择 **Import Git Repository** → 选中本项目
2. Framework Preset 选 **Other** 即可（Vercel 会自动识别 Python / FastAPI 框架）
3. 直接 **Deploy**（`vercel.json` 仅配置函数超时 `maxDuration: 300`）
   ⚠️ 不要添加 `rewrites` 路由重写：Vercel 的 FastAPI 框架路由会按原始路径转发请求，
       若在 vercel.json 里配置 `"rewrites": [{"source": "/(.*)", "destination":
       "/api/index.py"}]`，应用收到的路径会变成 `/api/index.py` 而非原始路径，
       导致全部路由 404 `{"detail":"Not Found"}`。
4. 也可用 CLI：`npm i -g vercel && vercel --prod`

> 提示：Vercel 函数是 Serverless 环境，**不配置持久化存储时任务状态存在进程内存、
> 结果文件写 /tmp，多实例/回收后会 404**。首次部署后请务必按下一节配置
> Upstash Redis + Vercel Blob（本项目已内置两种模式的自动切换）。

### 3. 配置持久化存储（必做，解决下载 / 地图不显示的根因）

Vercel 函数是无状态、按请求隔离、实例短暂的环境。本地运行依赖的「内存任务表 +
后台线程 + /tmp 文件」在线上全部失效（表现为下载提示“无法从网站上获取文件”、
地图瓦片加载不出来）。本项目通过两个官方服务修复：

1. **Upstash Redis（存任务状态/进度，跨实例共享）**
   - 在 [Upstash Console](https://console.upstash.com/) 创建免费数据库（选 `Global`，
     地域选离你最近的，如新加坡）
   - 或直接在 Vercel 项目 **Storage → Create Database → Upstash Redis** 创建并关联
   - 会生成两个环境变量：`UPSTASH_REDIS_REST_URL`、`UPSTASH_REDIS_REST_TOKEN`

2. **Vercel Blob（存结果文件 before/after/change_map.tif + geojson + zip）**
   - Vercel 项目 **Storage → Create Database → Blob**，Access 选 **Public**
   - 会生成环境变量：`BLOB_READ_WRITE_TOKEN`

3. 部署后访问 **`/api/diag`** 确认：
   `"storage": {"mode": "redis+blob", "redis": true, "blob": true}`。
   若 `mode` 显示 `local-memory`，说明环境变量没生效，请检查后重新部署。

> 两个服务的免费额度都够本 Demo 使用（Upstash Redis 免费 500MB 存储 / 每日
> 1 万次写请求；Vercel Blob Hobby 免费 5GB 存储 + 20GB/月带宽）。
> 函数单次执行有时长上限（Hobby 默认 60s），较大的监测范围一次请求可能跑不完，
> 本应用会自动「分片接管」：轮询请求发现任务没有新进度时会在本次请求内继续执行，
> 被超时打断后下一次轮询接着跑，直到完成。建议把监测范围控制在单边 0.1°~0.2° 以内。


### 网络受限时：不经过 GitHub，直接部署到 Vercel

Vercel **不强制要求 GitHub**，以下三种方式都可直接部署：

**方式 A：Vercel CLI 直接上传（推荐）**

```bash
npm i -g vercel          # 需要 Node.js（本机已有）
vercel login             # 浏览器授权，或用 vercel login --token <TOKEN>
                         # token 在 https://vercel.com/account/tokens 生成
vercel --prod
```

CLI 会把项目文件直接上传到 Vercel（不依赖 Git）。上传走 HTTPS，若网络不畅
可给 CLI 配代理：`$env:HTTPS_PROXY='http://127.0.0.1:10809'` 后重试。

**方式 B：换 Git 托管商中转**

GitHub 连接不畅时，把仓库推到 GitLab / Bitbucket / Gitee，
再在 Vercel **Import Git Repository** 中选择对应平台即可（Vercel 三者都支持）。

**方式 C：网页 Drag & Drop**

`vercel.com/new` 直接拖拽上传文件夹——但该方式主要支持静态/前端框架项目，
**对 Python Serverless 函数支持有限，本项目不建议使用**。

> 无论用哪种方式，部署后都需要配置「第 3 节」的两个存储环境变量
> （`UPSTASH_REDIS_REST_URL/TOKEN` + `BLOB_READ_WRITE_TOKEN`）；数据本身来自
> Planetary Computer 公开 STAC API（匿名签名），`io-lulc-9-class` 为公开数据，
> 无需任何密钥即可拉取。

## API 文档

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 前端页面 |
| GET | `/api/meta` | 数据源 / 年份 / 类别元信息 |
| GET | `/api/diag` | 运行时诊断：引擎加载状态、当前存储后端（redis+blob / local-memory） |
| POST | `/api/jobs` | 创建任务，body：`{"bbox":[西,南,东,北], "year_before":2021, "year_after":2022}` |
| GET | `/api/jobs/{id}` | 轮询任务状态（`status/percent/logs/result`） |
| GET | `/api/jobs/{id}/download?fmt=tif\|geojson` | 下载结果 |
| GET | `/api/jobs/{id}/tiles/{layer}/{z}/{x}/{y}.png` | 瓦片图层（`layer` ∈ before/after/change），供前端叠加与卷帘 |

FastAPI 自动生成交互式文档：`/docs`。

## 架构演进建议

当前实现已解决 Serverless 无状态问题（任务状态 → Upstash Redis，结果文件 →
Vercel Blob），仍为 Demo 级实现，生产化可沿此路径继续演进：
1. **真异步队列**：目前靠“轮询请求接管执行 + 幂等重跑”分片完成，改为队列 Worker /
   Vercel Queues 更省资源、进度更平滑
2. **任务检查点**：接管重跑会重复下载两期影像，可给引擎加检查点（缓存已拉取的中间栅格）
3. **多瓦片 mosaic**：目前选择 bbox 中心点所在 UTM 瓦片；跨瓦片区域需按 STAC item 做 mosaic 拼接
4. **瓦片缓存**：目前为进程内内存缓存 + 浏览器缓存，可换 CDN / 对象存储
5. **任务清理**：Blob 里的结果文件目前保留不删，可加定时清理（Vercel Cron + 生命周期策略）

## 免责声明

- 数据来自 Microsoft Planetary Computer，遵循其使用条款；`io-lulc-9-class` 为
  Impact Observatory 的预训练分类产品，分类误差在相邻年份间会表现为"伪变化"，
  做长期监测建议跨多年对比并结合净变化指标
- 本项目仅用于学习与演示

## License

[MIT](./LICENSE)

