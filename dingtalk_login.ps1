# Native login identifiers observed in the installed DingTalk account-login window.
param([switch]$Probe)
$ErrorActionPreference = 'Stop'
$stage = 'INPUT'
function Find-NativeField($parent, [string]$identifier) {
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $identifier)
    $matches = $parent.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($matches.Count -ne 1) { return $null }
    return $matches[0]
}
function Get-FieldSetter($element) {
    $pattern = $null
    if ($element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        if ($pattern.Current.IsReadOnly) { throw 'MANUAL' }
        return $pattern
    }
    if ($element.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$pattern)) {
        return $pattern
    }
    throw 'MANUAL'
}
try {
    if ($Probe) {
        $probeData = [Console]::In.ReadLine() | ConvertFrom-Json
        $loginData = [pscustomobject]@{ authorized=$true; client=$probeData.client }
    } else {
        $loginData = [Console]::In.ReadLine() | ConvertFrom-Json
    }
    if ($loginData.authorized -ne $true) { [Console]::Write('MANUAL'); exit 0 }
    $clientPath = [IO.Path]::GetFullPath($loginData.client)
    if ([IO.Path]::GetFileName($clientPath) -ine 'DingTalk.exe' -or -not [IO.File]::Exists($clientPath)) {
        [Console]::Write('MANUAL'); exit 0
    }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $stage = 'FIND_FORM'
    $ui = [System.Windows.Automation.AutomationElement]
    $prefix = 'loginView.widgetContainer.loginAccountPageView.contentWidget.contentWidgetInner.'
    $ready = $null
    for ($round = 0; $round -lt 12; $round++) {
        $clients = @(Get-Process DingTalk -ErrorAction SilentlyContinue | Where-Object { $_.Path -ieq $clientPath })
        $forms = @()
        foreach ($client in $clients) {
            if (-not $Probe -and $loginData.expected_pid) {
                if ($client.Id -ne $loginData.expected_pid -or $client.StartTime.ToUniversalTime().Ticks.ToString() -ne $loginData.expected_started) { continue }
            }
            $condition = [System.Windows.Automation.PropertyCondition]::new($ui::ProcessIdProperty, [int]$client.Id)
            $windows = $ui::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
            foreach ($window in $windows) {
                if ($window.Current.Name -notin @('钉钉', 'DingTalk')) { continue }
                if ($window.Current.ClassName -notin @('DtLoginView', 'StandardFrame_DingTalk')) { continue }
                if ($window.Current.ClassName -eq 'StandardFrame_DingTalk') {
                    $avatar = Find-NativeField $window 'avatar_btn'
                    $navigator = Find-NativeField $window 'navigator_view'
                    if ($avatar -and $navigator -and $avatar.Current.FrameworkId -eq 'Qt' -and $navigator.Current.FrameworkId -eq 'Qt') {
                        [Console]::Write('ALREADY_LOGGED_IN'); exit 0
                    }
                }
                $native = Find-NativeField $window 'loginView'
                if ($null -eq $native) { continue }
                # Embedded web pages expose Document descendants and do not qualify as this native form.
                $documents = $native.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                    [System.Windows.Automation.PropertyCondition]::new($ui::ControlTypeProperty, [System.Windows.Automation.ControlType]::Document))
                if ($documents.Count -ne 0) { continue }
                $account = Find-NativeField $native ($prefix + 'widgetCellInput.widgetContent.editMobile')
                $password = Find-NativeField $native ($prefix + 'widgetPassword.widgetContent.editPassword')
                $submit = Find-NativeField $native ($prefix + 'btnLogin')
                $autoLogin = Find-NativeField $native ($prefix + 'checkAutoLogin')
                if ($null -eq $account -or $null -eq $password -or $null -eq $submit -or $null -eq $autoLogin) { continue }
                if ($account.Current.IsPassword -or -not $password.Current.IsPassword) { continue }
                if (-not $account.Current.IsEnabled -or -not $password.Current.IsEnabled) { continue }
                if ($account.Current.IsOffscreen -or $password.Current.IsOffscreen -or $submit.Current.IsOffscreen) { continue }
                $forms += [pscustomobject]@{ Account=$account; Password=$password; Submit=$submit; Pid=$client.Id; Started=$client.StartTime.ToUniversalTime().Ticks.ToString() }
            }
        }
        if ($forms.Count -gt 1) { [Console]::Write('MANUAL'); exit 0 }
        if ($forms.Count -eq 1) { $ready=$forms[0]; break }
        Start-Sleep -Milliseconds 800
    }
    if ($null -eq $ready) { [Console]::Write('NO_FORM'); exit 0 }
    if ($Probe) {
        [pscustomobject]@{state='LOGIN_REQUIRED';pid=$ready.Pid;started=$ready.Started} | ConvertTo-Json -Compress | Write-Output
        exit 0
    }
    $stage = 'GET_SETTERS'
    $accountSetter = Get-FieldSetter $ready.Account
    $passwordSetter = Get-FieldSetter $ready.Password
    $submitPattern = $ready.Submit.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $stage = 'FILL_ACCOUNT'
    $accountSetter.SetValue([string]$loginData.account)
    $stage = 'FILL_PASSWORD'
    $passwordSetter.SetValue([string]$loginData.password)
    for ($round = 0; $round -lt 10 -and -not $ready.Submit.Current.IsEnabled; $round++) {
        Start-Sleep -Milliseconds 100
    }
    if (-not $ready.Submit.Current.IsEnabled) { [Console]::Write('MANUAL'); exit 0 }
    $stage = 'SUBMIT'
    $submitPattern.Invoke()
    [Console]::Write('SUBMITTED')
} catch {
    if ($Probe) { [Console]::Write(('PROBE_ERROR '+$stage+' '+$_.Exception.GetType().Name)); exit 1 }
    [Console]::Write('MANUAL')
    exit 1
} finally {
    $loginData = $null
}
