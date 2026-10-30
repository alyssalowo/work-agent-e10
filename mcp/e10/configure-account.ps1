$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$e10SetupRoot = $PSScriptRoot
Add-Type -AssemblyName System.Drawing
. "$PSScriptRoot\load-client.ps1"
[Windows.Forms.Application]::EnableVisualStyles()
$e10Form = New-Object Windows.Forms.Form
$e10Form.Text = '配置鼎捷 E10 MCP — 大寰公司（保存已修复）'
$e10Form.ClientSize = New-Object Drawing.Size(590, 355)
$e10Form.StartPosition = 'Manual'
$e10WorkingArea = [Windows.Forms.Screen]::FromPoint([Windows.Forms.Cursor]::Position).WorkingArea
$e10Form.Location = New-Object Drawing.Point(($e10WorkingArea.X + [Math]::Max(0, [int](($e10WorkingArea.Width - 610) / 2))), ($e10WorkingArea.Y + [Math]::Max(0, [int](($e10WorkingArea.Height - 395) / 2))))
$e10Form.WindowState = 'Normal'
$e10Form.TopMost = $true
$e10Form.ShowInTaskbar = $true
$e10Form.FormBorderStyle = 'FixedDialog'
$e10Form.MaximizeBox = $false
$e10Form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 10)
$e10Intro = New-Object Windows.Forms.Label
$e10Intro.Text = "服务器：$($e10ConnectionConfiguration.server_url)`r`n账套：$($e10ConnectionConfiguration.account_set_name)　认证：E10 用户身份认证"
$e10Intro.SetBounds(20, 15, 550, 60)
$e10Form.Controls.Add($e10Intro)
$e10UserLabel = New-Object Windows.Forms.Label
$e10UserLabel.Text = 'E10 账号'
$e10UserLabel.SetBounds(25, 90, 100, 28)
$e10Form.Controls.Add($e10UserLabel)
$e10UserBox = New-Object Windows.Forms.TextBox
$e10UserBox.SetBounds(135, 86, 410, 30)
$e10Form.Controls.Add($e10UserBox)
$e10PasswordLabel = New-Object Windows.Forms.Label
$e10PasswordLabel.Text = 'E10 密码'
$e10PasswordLabel.SetBounds(25, 135, 100, 28)
$e10Form.Controls.Add($e10PasswordLabel)
$e10PasswordBox = New-Object Windows.Forms.TextBox
$e10PasswordBox.UseSystemPasswordChar = $true
$e10PasswordBox.SetBounds(135, 131, 410, 30)
$e10Form.Controls.Add($e10PasswordBox)
$e10Notice = New-Object Windows.Forms.Label
$e10Notice.Text = '验证成功后使用 Windows 加密保存，仅供当前 Windows 用户使用。'
$e10Notice.SetBounds(25, 180, 535, 30)
$e10Form.Controls.Add($e10Notice)
$e10StatusLabel = New-Object Windows.Forms.Label
$e10StatusLabel.Text = '请输入你平时登录 E10 使用的账号密码，不要发送到聊天。'
$e10StatusLabel.SetBounds(25, 215, 535, 48)
$e10Form.Controls.Add($e10StatusLabel)
$e10SaveButton = New-Object Windows.Forms.Button
$e10SaveButton.Text = '验证并保存'
$e10SaveButton.SetBounds(335, 285, 125, 38)
$e10Form.Controls.Add($e10SaveButton)
$e10CancelButton = New-Object Windows.Forms.Button
$e10CancelButton.Text = '关闭'
$e10CancelButton.SetBounds(475, 285, 80, 38)
$e10CancelButton.Add_Click({ $e10Form.Close() })
$e10Form.Controls.Add($e10CancelButton)
$e10Form.AcceptButton = $e10SaveButton
$e10CredentialFile = "$PSScriptRoot\credentials.dpapi"
if (Test-Path -LiteralPath $e10CredentialFile) {
    try { $e10UserBox.Text = [E10CredentialStore]::Load($e10CredentialFile).UserName } catch {}
}
$e10SaveButton.Add_Click({
    $e10SetupPhase = 'check_input'
    if ([string]::IsNullOrWhiteSpace($e10UserBox.Text) -or [string]::IsNullOrEmpty($e10PasswordBox.Text)) {
        $e10StatusLabel.Text = '请填写账号和密码。'
        return
    }
    $e10SaveButton.Enabled = $false
    $e10StatusLabel.ForeColor = [Drawing.Color]::Black
    $e10StatusLabel.Text = '正在验证 E10 登录，请稍候……'
    $e10Form.Refresh()
    $e10SetupSession = $null
    try {
        $e10SetupPhase = 'create_connection'
        $e10SetupSession = New-Object -TypeName E10AccountSession -ArgumentList $e10ConnectionConfiguration.server_url
        $e10SetupPhase = 'login'
        $e10RawResult = $e10SetupSession.Login($e10UserBox.Text.Trim(), $e10PasswordBox.Text, $e10ConnectionConfiguration.language)
        $e10SetupPhase = 'parse_login_response'
        $e10SetupResult = $e10RawResult | ConvertFrom-Json
        if ($e10SetupResult.ok -and $e10SetupResult.authenticated) {
            $e10SetupPhase = 'encrypt_credential'
            $e10SetupPhase = 'save_encrypted_credential'
            [E10CredentialStore]::Save($e10CredentialFile, $e10UserBox.Text.Trim(), $e10PasswordBox.Text)
            $e10SetupResult | Add-Member -NotePropertyName account_set_name -NotePropertyValue $e10ConnectionConfiguration.account_set_name
            $e10SetupResult | Add-Member -NotePropertyName saved_at -NotePropertyValue ([DateTimeOffset]::Now.ToString('o'))
            $e10SetupResult | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$PSScriptRoot\account-setup-result.json" -Encoding UTF8
            $e10PasswordBox.Clear()
            $e10StatusLabel.ForeColor = [Drawing.Color]::DarkGreen
            $e10StatusLabel.Text = '登录验证成功，账号已在本机加密保存。可以关闭此窗口。'
            $e10SaveButton.Text = '已保存'
        } else {
            $e10SetupResult | Add-Member -NotePropertyName phase -NotePropertyValue $e10SetupPhase -Force
            $e10SetupResult | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $e10SetupRoot 'account-setup-result.json') -Encoding UTF8
            $e10StatusLabel.ForeColor = [Drawing.Color]::DarkRed
            $e10StatusLabel.Text = '登录未通过，请核对账号密码；未保存本次输入。'
            [Windows.Forms.MessageBox]::Show([string]$e10SetupResult.error, 'E10 登录结果') | Out-Null
            $e10SaveButton.Enabled = $true
        }
    } catch {
        $e10SetupError = $_.Exception.GetBaseException()
        $e10SetupErrorMessage = $e10SetupError.Message
        if ($e10PasswordBox.Text) { $e10SetupErrorMessage = $e10SetupErrorMessage.Replace($e10PasswordBox.Text, '[已隐藏]') }
        @{ ok=$false; authenticated=$false; phase=$e10SetupPhase; error_type=$e10SetupError.GetType().FullName; error=$e10SetupErrorMessage } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $e10SetupRoot 'account-setup-result.json') -Encoding UTF8
        $e10StatusLabel.ForeColor = [Drawing.Color]::DarkRed
        $e10StatusLabel.Text = '连接未完成，具体原因已记录。请将窗口保留，回到聊天即可。'
        $e10SaveButton.Enabled = $true
    } finally {
        $e10SecurePassword = $null
        $e10Credential = $null
        if ($e10SetupSession) { $e10SetupSession.Logout() | Out-Null; $e10SetupSession.Dispose() }
    }
})
$e10Form.Add_Shown({
    $e10Form.Activate()
    $e10Form.BringToFront()
    $e10UserBox.Focus()
    @{ shown=$true; process_id=$PID; title=$e10Form.Text } | ConvertTo-Json -Compress | Set-Content -LiteralPath "$PSScriptRoot\setup-window-status.json" -Encoding UTF8
})
$e10Form.ShowDialog() | Out-Null
$e10PasswordBox.Clear()
$e10Form.Dispose()
