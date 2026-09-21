$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' 'src\cuhk_shenzhen_web2api\scripts\server.py' '--port' '8766'
