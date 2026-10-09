package main

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Cache the OS boot identity, not the Cockpit process start time. Reopening
// Cockpit retains takeover, whereas a new Windows boot defaults to ordinary.
var fenjueBootID = sync.OnceValues(func() (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks")
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	raw, err := command.Output()
	if err != nil {
		return "", errors.New("cannot determine Windows boot identity")
	}
	value := strings.TrimSpace(string(raw))
	if len(value) < 10 || strings.Trim(value, "0123456789") != "" {
		return "", errors.New("invalid Windows boot identity")
	}
	return value, nil
})
