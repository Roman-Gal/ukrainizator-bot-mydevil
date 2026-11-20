Option Explicit
Dim shell, fs, folder, pythonw
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
pythonw = folder & "\.venv\Scripts\pythonw.exe"
If Not fs.FileExists(pythonw) Then
    MsgBox "Python environment not found. See README.md for installation.", 16, "Bot Control"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
shell.Run Chr(34) & pythonw & Chr(34) & " -X utf8 -m bot_control --start", 0, False
