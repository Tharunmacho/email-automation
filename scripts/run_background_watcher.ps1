# Persistent Auto-Restarting Background Supervisor for Resume Ingestion Watcher
# Runs `python -m app.cli watch` continuously. If interrupted or killed, it auto-restarts immediately.

$ProjectDir = "m:\gmail automation"
Set-Location $ProjectDir

Write-Host "=========================================================" -ForegroundColor Green
Write-Host " Starting Persistent Resume Ingestion Background Supervisor" -ForegroundColor Green
Write-Host "=========================================================" -ForegroundColor Green

$PollInterval = 60

while ($true) {
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Spawning Gmail Watcher Process (interval=${PollInterval}s)..." -ForegroundColor Cyans
    
    try {
        $process = Start-Process -FilePath "python" -ArgumentList "-m app.cli watch --interval $PollInterval" -PassThru -NoNewWindow
        
        # Monitor the running process
        $process.WaitForExit()
        
        Write-Warning "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Watcher process exited with code $($process.ExitCode). Restarting in 5 seconds..."
    } catch {
        Write-Error "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Exception while running watcher: $_"
    }

    Start-Sleep -Seconds 5
}
