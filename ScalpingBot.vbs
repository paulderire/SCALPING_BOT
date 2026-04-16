' ScalpingBot Desktop App Launcher
' Double-click this file to start the desktop dashboard app with no terminal window.

Dim oShell, oFSO, sDir, sPsLauncher, cmd

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

' Get the folder this VBS lives in
sDir = oFSO.GetParentFolderName(WScript.ScriptFullName)

' Launch start_desktop_app.ps1 silently
sPsLauncher = sDir & "\start_desktop_app.ps1"
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & sPsLauncher & """"
oShell.Run cmd, 0, False

WScript.Quit
