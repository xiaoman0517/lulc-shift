# 一键重新部署到 Vercel
# 用法：
#   .\deploy.ps1 -Token vcp_xxxxx          # 直接传 token
#   $env:VERCEL_TOKEN="vcp_xxxxx"; .\deploy.ps1   # 或用环境变量
# 注意：项目已与 Vercel 链接（.vercel/project.json 存在），无需再次初始化。
param(
    [string]$Token = $env:VERCEL_TOKEN
)

$ErrorActionPreference = "Stop"
$env:NO_PROXY = "*"
$env:no_proxy = "*"

# 定位 vercel CLI（优先 PATH，其次本机已知的全局安装位置）
$cli = $null
$cmd = Get-Command vercel -ErrorAction SilentlyContinue
if ($cmd) {
    $cli = $cmd.Source
} else {
    $candidates = @(
        "C:\Users\19154\scoop\persist\nodejs\bin\node_modules\vercel\dist\index.js",
        "$env:APPDATA\npm\node_modules\vercel\dist\index.js"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $cli = $c; break }
    }
}
if (-not $cli) {
    Write-Host "未找到 vercel CLI，请先运行：npm i -g vercel" -ForegroundColor Yellow
    exit 1
}

if (-not $Token) {
    Write-Host "缺少 token。用法：.\deploy.ps1 -Token vcp_xxxxx （或先设置 \$env:VERCEL_TOKEN）" -ForegroundColor Yellow
    exit 1
}

$node = (Get-Command node).Source
Write-Host "==> 开始部署到 Vercel（Production）..." -ForegroundColor Cyan
if ($cli.EndsWith(".js")) {
    & $node $cli --prod --yes --token $Token
} else {
    & $cli --prod --yes --token $Token
}

Write-Host ""
Write-Host "部署完成。访问：https://land-cover-analysis.vercel.app" -ForegroundColor Green
