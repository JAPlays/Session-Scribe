<#
.SYNOPSIS
  Builds an offline Session Scribe installer: the program, its own Python with WhisperX, the built-in AI
  (llama.cpp's llama-server), FFmpeg, and every model, so installing needs no internet and no setup.

.EXAMPLE
  # once: install Inno Setup 6 (https://jrsoftware.org/isdl.php), then from this folder:
  $env:HF_TOKEN = "hf_..."      # a Hugging Face token whose account accepted the pyannote model terms
  .\build.ps1 -Edition mistral-24b

.NOTES
  Downloads about 20 GB the first time (kept in build\cache, so rebuilding is quick). No patient data is
  involved. The result is dist\SessionScribe-<edition>-<build>-setup.exe plus its .bin parts: keep them
  together in one folder (copy the whole folder to a USB drive to hand it to someone).
#>
param(
  [string]$Edition = "mistral-24b",
  [string]$HfToken = $env:HF_TOKEN,
  # Pinned, tested-together versions (checked against their release workflows). "latest" asks GitHub instead.
  [string]$LlamaTag = "b11900",            # llama.cpp release, 2026-10-26
  [string]$PythonTag = "20261026",         # python-build-standalone release, 2026-10-26 ...
  [string]$PythonVersion = "3.12.16",      # ... and the CPython 3.12 it ships
  [string]$PythonMinor = "3.12",           # (only used with -PythonTag latest)
  [string]$Iscc = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is many times faster without the bar

$Here = $PSScriptRoot
$Repo = Split-Path $Here -Parent
$Editions = Get-Content (Join-Path $Here "editions.json") -Raw | ConvertFrom-Json
$Ed = $Editions.$Edition
if (-not $Ed) { throw "No edition '$Edition' in editions.json." }

$Build = Join-Path $Repo "build"
$Cache = Join-Path $Build "cache"
$Stage = Join-Path $Build $Edition
$Dist = Join-Path $Repo "dist"
New-Item -ItemType Directory -Force $Cache, $Dist | Out-Null
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force $Stage | Out-Null

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function GitHubRelease($repo, $tag) {
  $url = if ($tag -eq "latest") { "https://api.github.com/repos/$repo/releases/latest" }
         else { "https://api.github.com/repos/$repo/releases/tags/$tag" }
  Invoke-RestMethod $url -Headers @{ "User-Agent" = "session-scribe-build" }
}
function Fetch($url, $out) {
  # curl.exe (built into Windows 10+) resumes big downloads and is much faster than Invoke-WebRequest
  if (Test-Path $out) { Write-Host "  cached: $(Split-Path $out -Leaf)"; return }
  Write-Host "  downloading $(Split-Path $out -Leaf)"
  $curlArgs = @("-L", "--fail", "--retry", "5", "-C", "-", "-o", "$out.part", $url)
  if ($url -like "https://huggingface.co/*" -and $HfToken) { $curlArgs = @("-H", "Authorization: Bearer $HfToken") + $curlArgs }
  & curl.exe @curlArgs
  if ($LASTEXITCODE) { throw "Download failed: $url" }
  Move-Item -Force "$out.part" $out
}

# ---------------------------------------------------------------- the program
Step "Program files"
$AppBuild = (Select-String -Path (Join-Path $Repo "session_scribe.py") -Pattern '^APP_BUILD = "([^"]+)"').Matches[0].Groups[1].Value
Copy-Item (Join-Path $Repo "session_scribe.py"), (Join-Path $Repo "engine_check.py"), (Join-Path $Repo "README.md") $Stage
Write-Host "  build $AppBuild"

# ---------------------------------------------------------------- Python (with tkinter)
Step "Python $PythonMinor"
if ($PythonTag -eq "latest") {
  $rel = GitHubRelease "astral-sh/python-build-standalone" "latest"
  $pat = "^cpython-$([regex]::Escape($PythonMinor))\.\d+\+\d+-x86_64-pc-windows-msvc-install_only\.tar\.gz$"
  $asset = $rel.assets | Where-Object { $_.name -match $pat } | Select-Object -First 1
  if (-not $asset) { throw "No Windows Python $PythonMinor build in python-build-standalone $($rel.tag_name)." }
  $pyName, $pyUrl = $asset.name, $asset.browser_download_url
} else {
  $pyName = "cpython-$PythonVersion+$PythonTag-x86_64-pc-windows-msvc-install_only.tar.gz"
  $pyUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PythonTag/$([uri]::EscapeDataString($pyName))"
}
$pyTar = Join-Path $Cache $pyName
Fetch $pyUrl $pyTar
tar -xzf $pyTar -C $Stage                         # makes $Stage\python
$Py = Join-Path $Stage "python\python.exe"
$PyVersion = (& $Py -c "import sys;print(sys.version.split()[0])")
& $Py -c "import tkinter" ; if ($LASTEXITCODE) { throw "That Python build has no tkinter." }
Write-Host "  Python $PyVersion"

Step "Python packages (speech engine and friends)"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
& $Py -m pip install --upgrade pip --no-warn-script-location
$pipArgs = @("-m", "pip", "install", "--no-warn-script-location") + $Ed.packages
if ($Ed.torch -and $Ed.torch -ne "cpu") { $pipArgs += @("--extra-index-url", "https://download.pytorch.org/whl/$($Ed.torch)") }
& $Py @pipArgs
if ($LASTEXITCODE) { throw "pip failed." }

Step "FFmpeg"
New-Item -ItemType Directory -Force (Join-Path $Stage "bin") | Out-Null
$ff = & $Py -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"
Copy-Item $ff (Join-Path $Stage "bin\ffmpeg.exe")

# ---------------------------------------------------------------- the built-in AI
Step "llama.cpp ($($Ed.llama_backend))"
# The win-<backend> zip has the whole toolset (llama-server, llama-gguf-split, the CPU backend) plus that
# backend; without a working Vulkan driver it simply runs on the processor.
if ($LlamaTag -eq "latest") { $LlamaVersion = (GitHubRelease "ggml-org/llama.cpp" "latest").tag_name } else { $LlamaVersion = $LlamaTag }
$zipName = "llama-$LlamaVersion-bin-win-$($Ed.llama_backend)-x64.zip"
$zip = Join-Path $Cache $zipName
Fetch "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaVersion/$zipName" $zip
$Engine = Join-Path $Stage "engine"
Expand-Archive $zip -DestinationPath $Engine -Force
$inner = Get-ChildItem $Engine -Recurse -Filter "llama-server.exe" | Select-Object -First 1
if (-not $inner) { throw "llama-server.exe isn't in $zipName." }
if ($inner.DirectoryName -ne $Engine) { Get-ChildItem $inner.DirectoryName | Move-Item -Destination $Engine -Force }
Write-Host "  llama.cpp $LlamaVersion"

# ---------------------------------------------------------------- AI model(s)
Step "AI model"
$LlmDir = Join-Path $Stage "models\llm"
New-Item -ItemType Directory -Force $LlmDir | Out-Null
$Models = @()
foreach ($m in $Ed.llm) {
  $info = Invoke-RestMethod "https://huggingface.co/api/models/$($m.repo)"
  $file = $info.siblings.rfilename | Where-Object { $_ -match "$([regex]::Escape($m.quant))\.gguf$" -and $_ -notmatch "mmproj" } |
          Select-Object -First 1
  if (-not $file) { throw "No $($m.quant) file in $($m.repo)." }
  $src = Join-Path $Cache (Split-Path $file -Leaf)
  Fetch "https://huggingface.co/$($m.repo)/resolve/main/$file" $src
  $stem = [IO.Path]::GetFileNameWithoutExtension($src)
  # split into parts under 2 GB: the installer handles those best, and llama-server loads them as one
  & (Join-Path $Engine "llama-gguf-split.exe") --split --split-max-size 1900M $src (Join-Path $LlmDir $stem)
  if ($LASTEXITCODE) { throw "Couldn't split $stem." }
  $Models += @{ name = $stem; repo = $m.repo; license = $m.license }
  Write-Host "  $stem"
}

# ---------------------------------------------------------------- speech, alignment and speaker models
Step "Speech and speaker models"
@{ edition = $Edition; staging = $true } | ConvertTo-Json | Set-Content (Join-Path $Stage "bundle.json") -Encoding UTF8
if (-not $HfToken) {
  Write-Warning "No HF_TOKEN: the installer will work, but without speaker labels (the pyannote model needs a token to download once, here)."
}
foreach ($w in $Ed.whisper_models) {
  $env:HF_TOKEN = $HfToken
  & $Py (Join-Path $Stage "session_scribe.py") --download-models --whisper-model $w --language $Ed.language
  if ($LASTEXITCODE) { throw "Model download failed (for speaker labels, accept the pyannote terms on huggingface.co)." }
}
Remove-Item Env:\HF_TOKEN -ErrorAction SilentlyContinue
# no tokens or download locks in what gets installed
Get-ChildItem (Join-Path $Stage "models") -Recurse -Include "token", "stored_tokens", "*.lock" -File | Remove-Item -Force
Get-ChildItem $Stage -Recurse -Directory -Filter "__pycache__" | Where-Object { $_.FullName -notlike "*\python\*" } |
  Remove-Item -Recurse -Force

# ---------------------------------------------------------------- what's inside
Step "bundle.json and notices"
[ordered]@{
  edition = $Edition; title = $Ed.title; for = $Ed.for; min_ram_gb = $Ed.min_ram_gb
  app_build = $AppBuild; built = (Get-Date -Format "yyyy-MM-dd")
  python = $PyVersion; llama_cpp = $LlamaVersion; llama_backend = $Ed.llama_backend
  default_model = $Models[0].name; models = $Models
  whisper_model = $Ed.whisper_models[0]
} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Stage "bundle.json") -Encoding UTF8
$notice = (Get-Content (Join-Path $Here "THIRD-PARTY-NOTICES.txt") -Raw)
$notice += "`r`nIn this installer: Python $PyVersion, llama.cpp $LlamaVersion ($($Ed.llama_backend)), " +
           (($Models | ForEach-Object { "$($_.name) from $($_.repo) ($($_.license))" }) -join ", ") + ".`r`n"
Set-Content (Join-Path $Stage "THIRD-PARTY-NOTICES.txt") $notice -Encoding UTF8

# ---------------------------------------------------------------- the installer
Step "Installer (Inno Setup)"
if (-not $Iscc) {
  $Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "Inno Setup 6 isn't installed (https://jrsoftware.org/isdl.php)." }
& $Iscc "/DStage=$Stage" "/DOutDir=$Dist" "/DEdition=$Edition" "/DEditionTitle=$($Ed.title)" `
        "/DAppBuild=$AppBuild" "/DMinRamGB=$($Ed.min_ram_gb)" (Join-Path $Here "SessionScribe.iss")
if ($LASTEXITCODE) { throw "Inno Setup failed." }
$size = (Get-ChildItem $Dist -Filter "SessionScribe-$Edition-$AppBuild-setup*" | Measure-Object Length -Sum).Sum / 1GB
Write-Host ("`nDone: {0}\SessionScribe-{1}-{2}-setup.exe (+ .bin parts), {3:N1} GB in all." -f $Dist, $Edition, $AppBuild, $size) -ForegroundColor Green
