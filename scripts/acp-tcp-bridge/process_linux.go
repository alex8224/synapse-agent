//go:build linux

package main

import (
	"os"
	"os/exec"
	"syscall"
)

func terminationSignal() os.Signal { return syscall.SIGTERM }

func configureChild(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	cmd.Cancel = func() error {
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if err == syscall.ESRCH {
			return os.ErrProcessDone
		}
		return err
	}
}

func cleanupChild(cmd *exec.Cmd) {
	// The leader may exit while grandchildren still own inherited descriptors.
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
}
