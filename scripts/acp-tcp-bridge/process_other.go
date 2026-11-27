//go:build !linux

package main

import (
	"os"
	"os/exec"
)

func terminationSignal() os.Signal { return os.Interrupt }

// The client is cross-platform. Deploy serve on Linux for process-group cleanup.
func configureChild(cmd *exec.Cmd) {}
func cleanupChild(cmd *exec.Cmd)   {}
