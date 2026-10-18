-- Client half of the INTERLOPER addon: closes the console after a command.
net.Receive("interloper_hide_ui", function()
    gui.HideGameUI()
end)
