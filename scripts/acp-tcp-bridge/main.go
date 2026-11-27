// ACP stdio <-> TCP bridge. No shell or third-party dependencies.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const maxFrame = 50 * 1024 * 1024

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "acp-tcp-bridge:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: acp-tcp-bridge client|serve [options]")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, terminationSignal())
	defer cancel()
	flags := flag.NewFlagSet(args[0], flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	tokenFile := flags.String("token-file", "", "shared secret file (32-256 non-whitespace bytes)")
	switch args[0] {
	case "client":
		addr := flags.String("connect", "127.0.0.1:19091", "TCP server address")
		timeout := flags.Duration("connect-timeout", 5*time.Second, "connection and authentication budget")
		hostRoot := flags.String("host-root", "", "host workspace root to map to --guest-root")
		guestRoot := flags.String("guest-root", "", "existing guest workspace root")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		if flags.NArg() != 0 || *timeout <= 0 {
			return errors.New("unexpected arguments or invalid timeout")
		}
		mapper, err := newCwdMapper(*hostRoot, *guestRoot)
		if err != nil {
			return err
		}
		token, err := readToken(*tokenFile)
		if err != nil {
			return err
		}
		return client(ctx, *addr, token, *timeout, mapper, os.Stdin, os.Stdout)
	case "serve":
		addr := flags.String("listen", "127.0.0.1:19091", "listen address; non-loopback requires --allow-remote")
		allowRemote := flags.Bool("allow-remote", false, "allow non-loopback guest listener (trusted private network only)")
		cwd := flags.String("workdir", "", "existing guest working directory")
		maxClients := flags.Int("max-clients", 16, "maximum simultaneous agent processes")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		if flags.NArg() == 0 || *maxClients < 1 || *maxClients > 128 {
			return errors.New("serve requires [options] -- executable [args]; max-clients must be 1-128")
		}
		if !filepath.IsAbs(flags.Arg(0)) {
			return errors.New("agent executable must be an absolute path")
		}
		if *cwd != "" {
			info, err := os.Stat(*cwd)
			if err != nil || !filepath.IsAbs(*cwd) || !info.IsDir() {
				return errors.New("workdir must be an existing absolute directory")
			}
		}
		host, _, err := net.SplitHostPort(*addr)
		if err != nil {
			return errors.New("invalid listen address")
		}
		ip := net.ParseIP(host)
		if !*allowRemote && (ip == nil || !ip.IsLoopback()) {
			return errors.New("non-loopback listener requires --allow-remote")
		}
		token, err := readToken(*tokenFile)
		if err != nil {
			return err
		}
		ln, err := net.Listen("tcp", *addr)
		if err != nil {
			return err
		}
		fmt.Fprintln(os.Stderr, "ACP bridge listening on", ln.Addr())
		return serve(ctx, ln, token, flags.Args(), *cwd, *maxClients)
	default:
		return errors.New("expected client or serve")
	}
}

func readToken(name string) (string, error) {
	f, err := os.Open(name)
	if err != nil {
		return "", errors.New("cannot open token file")
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, 258))
	token := strings.TrimSpace(string(data))
	if err != nil || len(data) > 257 || len(token) < 32 || len(token) > 256 || strings.ContainsAny(token, "\r\n\t ") {
		return "", errors.New("token must contain 32-256 non-whitespace bytes")
	}
	return token, nil
}

func client(ctx context.Context, addr, token string, timeout time.Duration, mapper *cwdMapper, input io.Reader, output io.Writer) error {
	dialer := net.Dialer{Timeout: timeout}
	conn, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	if err := conn.SetDeadline(time.Now().Add(timeout)); err != nil {
		return err
	}
	if _, err := io.WriteString(conn, token+"\n"); err != nil {
		return errors.New("authentication write failed")
	}
	reader := bufio.NewReaderSize(conn, 512)
	reply, err := reader.ReadSlice('\n')
	if err != nil || string(reply) != "OK\n" {
		return errors.New("authentication failed or agent could not start")
	}
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return err
	}
	inputDone := make(chan error, 1)
	go func() {
		err := copyInput(conn, input, mapper)
		inputDone <- err
		if err != nil {
			conn.Close()
		} else if tcp, ok := conn.(*net.TCPConn); ok {
			tcp.CloseWrite()
		}
	}()
	_, outputErr := io.Copy(output, reader)
	// A remote exit must terminate the stdio adapter even while stdin is still open.
	conn.Close()
	select {
	case err := <-inputDone:
		if err != nil {
			return err
		}
	default:
	}
	if ctx.Err() != nil {
		return nil
	}
	return outputErr
}

func serve(ctx context.Context, ln net.Listener, token string, argv []string, cwd string, capacity int) error {
	ctx, cancel := context.WithCancel(ctx)
	defer ln.Close()
	stop := context.AfterFunc(ctx, func() { ln.Close() })
	defer stop()
	var wg sync.WaitGroup
	defer wg.Wait()
	defer cancel()
	slots := make(chan struct{}, capacity)
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		select {
		case slots <- struct{}{}:
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer func() { <-slots }()
				if err := serveConn(ctx, conn, token, argv, cwd); err != nil && ctx.Err() == nil {
					// Never include ACP payloads, authentication values or child stderr.
					fmt.Fprintln(os.Stderr, "ACP connection ended:", err)
				}
			}()
		default:
			conn.Close()
		}
	}
}

type gatedWriter struct {
	ready   <-chan struct{}
	out     io.Writer
	onError context.CancelFunc
}

func (w gatedWriter) Write(p []byte) (int, error) {
	<-w.ready
	n, err := w.out.Write(p)
	if err != nil {
		w.onError()
	}
	return n, err
}

func serveConn(parent context.Context, conn net.Conn, token string, argv []string, cwd string) error {
	defer conn.Close()
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	conn.SetDeadline(time.Now().Add(5 * time.Second))
	reader := bufio.NewReaderSize(conn, 512)
	line, err := reader.ReadSlice('\n')
	if err != nil || subtle.ConstantTimeCompare(bytes.TrimSuffix(line, []byte{'\n'}), []byte(token)) != 1 {
		return errors.New("authentication rejected")
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = cwd
	// Child diagnostics may contain credentials. Do not forward or persist them.
	cmd.Stderr = io.Discard
	ready := make(chan struct{})
	cmd.Stdout = gatedWriter{ready, conn, cancel}
	cmd.WaitDelay = 2 * time.Second
	configureChild(cmd)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		close(ready)
		return errors.New("agent stdin unavailable")
	}
	defer stdin.Close()
	if err := cmd.Start(); err != nil {
		close(ready)
		return errors.New("agent start failed")
	}
	_, authErr := io.WriteString(conn, "OK\n")
	close(ready)
	if authErr != nil {
		cancel()
	} else {
		conn.SetDeadline(time.Time{})
	}
	go func() {
		io.Copy(stdin, reader)
		stdin.Close()
		// EOF means the host no longer owns this agent; kill its entire process group.
		cancel()
	}()
	err = cmd.Wait()
	cleanupChild(cmd)
	if ctx.Err() != nil {
		return nil
	}
	if err != nil {
		return errors.New("agent exited unsuccessfully")
	}
	return nil
}

type cwdMapper struct {
	hostRoot  string
	guestRoot string
}

func normalizeHost(value string) string {
	return strings.TrimRight(strings.ReplaceAll(value, "\\", "/"), "/")
}

func newCwdMapper(hostRoot, guestRoot string) (*cwdMapper, error) {
	if hostRoot == "" && guestRoot == "" {
		return nil, nil
	}
	if hostRoot == "" || !path.IsAbs(guestRoot) || strings.Contains(guestRoot, "\\") {
		return nil, errors.New("cwd mapping requires host-root and an absolute POSIX guest-root")
	}
	if !filepath.IsAbs(hostRoot) {
		return nil, errors.New("host-root must be absolute")
	}
	return &cwdMapper{normalizeHost(hostRoot), path.Clean(guestRoot)}, nil
}

func (m *cwdMapper) rewrite(frame []byte) ([]byte, error) {
	var msg map[string]json.RawMessage
	if json.Unmarshal(frame, &msg) != nil {
		return nil, errors.New("invalid ACP JSON frame")
	}
	var method string
	json.Unmarshal(msg["method"], &method)
	if method != "session/new" && method != "session/load" {
		return frame, nil
	}
	var params map[string]json.RawMessage
	if json.Unmarshal(msg["params"], &params) != nil {
		return nil, errors.New("invalid ACP session parameters")
	}
	var cwd string
	if json.Unmarshal(params["cwd"], &cwd) != nil {
		return nil, errors.New("ACP session requires cwd")
	}
	normal := normalizeHost(cwd)
	// Only Windows drive paths are case-insensitive; POSIX paths stay case-sensitive.
	root, candidate := m.hostRoot, normal
	if len(root) >= 2 && root[1] == ':' {
		root, candidate = strings.ToLower(root), strings.ToLower(candidate)
	}
	if candidate != root && !strings.HasPrefix(candidate, root+"/") {
		return nil, errors.New("ACP cwd is outside configured host-root")
	}
	relative := strings.TrimPrefix(normal[len(m.hostRoot):], "/")
	for _, segment := range strings.Split(relative, "/") {
		if segment == ".." || segment == "." {
			return nil, errors.New("ACP cwd contains traversal segments")
		}
	}
	mapped := path.Join(m.guestRoot, relative)
	params["cwd"], _ = json.Marshal(mapped)
	msg["params"], _ = json.Marshal(params)
	return json.Marshal(msg)
}

func copyInput(dst io.Writer, src io.Reader, mapper *cwdMapper) error {
	if mapper == nil {
		_, err := io.Copy(dst, src)
		return err
	}
	scanner := bufio.NewScanner(src)
	scanner.Buffer(make([]byte, 64*1024), maxFrame)
	for scanner.Scan() {
		frame, err := mapper.rewrite(scanner.Bytes())
		if err != nil {
			return err
		}
		if _, err := dst.Write(append(frame, '\n')); err != nil {
			return err
		}
	}
	if scanner.Err() != nil {
		return errors.New("ACP frame read failed or exceeded 50 MiB")
	}
	return nil
}
