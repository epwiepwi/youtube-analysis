param(
    [string]$Narration = "$HOME\Desktop\narration.wav",
    [string]$Clips = "$HOME\Desktop\clips",
    [string]$Name = "shorts_001",
    [string]$Model = "small",
    [string]$Device = "cpu"
)

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$env:WHISPER_DEVICE = $Device
$env:WHISPER_COMPUTE = "int8"
$env:WHISPER_MODEL = $Model
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Write-Host "narration: $Narration"
Write-Host "clips    : $Clips"
Write-Host "name     : $Name"
Write-Host "model    : $Model on $Device"
Write-Host ""

python -m src.main --narration "$Narration" --clips "$Clips" --name "$Name"
