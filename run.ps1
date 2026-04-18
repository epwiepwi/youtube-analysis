param(
    [string]$Narration = "$HOME\Desktop\나레이션.wav",
    [string]$Clips = "$HOME\Desktop\클립들",
    [string]$Name = "양파쇼츠_001",
    [string]$Model = "small",
    [string]$Device = "cpu"
)

$env:WHISPER_DEVICE = $Device
$env:WHISPER_COMPUTE = "int8"
$env:WHISPER_MODEL = $Model

Write-Host "narration: $Narration"
Write-Host "clips    : $Clips"
Write-Host "name     : $Name"
Write-Host "model    : $Model on $Device"
Write-Host ""

python -m src.main --narration "$Narration" --clips "$Clips" --name "$Name"
