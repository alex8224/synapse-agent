package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const testToken = "test-only-token-not-a-real-secret-123456"

func TestBridgeChild(t *testing.T) {
	mode := os.Args[len(os.Args)-1]
	if mode != "bridge-test-echo" && mode != "bridge-test-exit" {
		return
	}
	if mode == "bridge-test-exit" {
		fmt.Fprintln(os.Stdout, "CHILD_EXIT")
		os.Exit(0)
	}
	io.Copy(os.Stdout, os.Stdin)
	os.Exit(0)
}

func startTestServer(t *testing.T, mode string) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- serve(ctx, ln, testToken, []string{os.Args[0], "-test.run=^TestBridgeChild$", "--", mode}, "", 4)
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(5 * time.Second):
			t.Error("server did not stop and reap children")
		}
	})
	return ln.Addr().String()
}

func connectTest(t *testing.T, addr, token string) (net.Conn, *bufio.Reader) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	conn.SetDeadline(time.Now().Add(4 * time.Second))
	if _, err := io.WriteString(conn, token+"\n"); err != nil {
		t.Fatal(err)
	}
	return conn, bufio.NewReader(conn)
}

func TestStreamingAndConnectionIsolation(t *testing.T) {
	addr := startTestServer(t, "bridge-test-echo")
	for _, payload := range []string{"first session\n", "second session\n"} {
		conn, reader := connectTest(t, addr, testToken)
		if reply, err := reader.ReadString('\n'); err != nil || reply != "OK\n" {
			t.Fatalf("auth: %q, %v", reply, err)
		}
		// Fragmented input is forwarded without waiting for EOF or a full JSON frame.
		for _, fragment := range []string{payload[:3], payload[3:]} {
			if _, err := io.WriteString(conn, fragment); err != nil {
				t.Fatal(err)
			}
		}
		if reply, err := reader.ReadString('\n'); err != nil || reply != payload {
			t.Fatalf("echo: %q, %v", reply, err)
		}
	}
}

func TestAuthenticationRejectsWrongAndOversizedTokens(t *testing.T) {
	addr := startTestServer(t, "bridge-test-echo")
	for _, token := range []string{"wrong-token", strings.Repeat("x", 2048)} {
		_, reader := connectTest(t, addr, token)
		if reply, _ := reader.ReadString('\n'); reply != "" {
			t.Fatalf("unexpected unauthenticated output: %q", reply)
		}
	}
}

func TestClientRoundTripAndCancellation(t *testing.T) {
	addr := startTestServer(t, "bridge-test-echo")
	inputR, inputW := io.Pipe()
	outputR, outputW := io.Pipe()
	defer inputR.Close()
	defer inputW.Close()
	defer outputR.Close()
	defer outputW.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- client(ctx, addr, testToken, time.Second, nil, inputR, outputW) }()
	writeDone := make(chan error, 1)
	go func() { _, err := io.WriteString(inputW, "{\"id\":1}\n"); writeDone <- err }()
	readDone := make(chan string, 1)
	go func() { line, _ := bufio.NewReader(outputR).ReadString('\n'); readDone <- line }()
	select {
	case line := <-readDone:
		if line != "{\"id\":1}\n" {
			t.Fatalf("unexpected ACP output: %q", line)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("round trip blocked with stdin open")
	}
	if err := <-writeDone; err != nil {
		t.Fatal(err)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(4 * time.Second):
		t.Fatal("client did not cancel")
	}
}

func TestRemoteExitDoesNotWaitForStdinEOF(t *testing.T) {
	addr := startTestServer(t, "bridge-test-exit")
	inputR, inputW := io.Pipe()
	defer inputR.Close()
	defer inputW.Close()
	var output bytes.Buffer
	done := make(chan error, 1)
	go func() { done <- client(context.Background(), addr, testToken, time.Second, nil, inputR, &output) }()
	select {
	case err := <-done:
		if err != nil || output.String() != "CHILD_EXIT\n" {
			t.Fatalf("remote exit: output=%q error=%v", output.String(), err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("client hung after agent exited")
	}
}

func TestCwdMapping(t *testing.T) {
	host := filepath.Join(t.TempDir(), "workspace")
	mapper, err := newCwdMapper(host, "/workspace")
	if err != nil {
		t.Fatal(err)
	}
	for _, method := range []string{"session/new", "session/load"} {
		msg := map[string]any{"jsonrpc": "2.0", "id": 7, "method": method, "params": map[string]any{"cwd": filepath.Join(host, "sub"), "mcpServers": []any{}, "sessionId": "preserved"}}
		frame, _ := json.Marshal(msg)
		mapped, err := mapper.rewrite(frame)
		if err != nil {
			t.Fatal(err)
		}
		var got struct {
			ID     int `json:"id"`
			Params struct {
				Cwd       string `json:"cwd"`
				SessionID string `json:"sessionId"`
			} `json:"params"`
		}
		if err := json.Unmarshal(mapped, &got); err != nil || got.ID != 7 || got.Params.Cwd != "/workspace/sub" || got.Params.SessionID != "preserved" {
			t.Fatalf("bad mapping: %s, %v", mapped, err)
		}
	}
	for _, cwd := range []string{host + "-other", host + "/../escape", host + "/sub/../../escape"} {
		frame, _ := json.Marshal(map[string]any{"method": "session/new", "params": map[string]string{"cwd": cwd}})
		if _, err := mapper.rewrite(frame); err == nil {
			t.Fatalf("accepted out-of-root cwd: %s", cwd)
		}
	}
	frame := []byte(`{"id":1,"method":"initialize","params":{"protocolVersion":1}}`)
	if got, err := mapper.rewrite(frame); err != nil || !bytes.Equal(got, frame) {
		t.Fatal("modified non-session frame")
	}
}

func TestMappingRequiresBothRoots(t *testing.T) {
	if _, err := newCwdMapper("", "/workspace"); err == nil {
		t.Fatal("missing host root accepted")
	}
	if _, err := newCwdMapper(t.TempDir(), "relative"); err == nil {
		t.Fatal("relative guest root accepted")
	}
}

func TestFrameMappingAndTokenValidation(t *testing.T) {
	mapper, _ := newCwdMapper(t.TempDir(), "/workspace")
	var output bytes.Buffer
	if err := copyInput(&output, strings.NewReader("not-json\n"), mapper); err == nil {
		t.Fatal("invalid JSON accepted")
	}
	tokenFile := filepath.Join(t.TempDir(), "token")
	for _, token := range []string{"short", strings.Repeat("x", 300), testToken + "\nextra"} {
		if err := os.WriteFile(tokenFile, []byte(token), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := readToken(tokenFile); err == nil {
			t.Fatal("invalid token accepted")
		}
	}
	os.WriteFile(tokenFile, []byte(testToken+"\n"), 0600)
	if token, err := readToken(tokenFile); err != nil || token != testToken {
		t.Fatal("valid token rejected")
	}
}

func TestRealAgentInitialize(t *testing.T) {
	agent := os.Getenv("ACP_BRIDGE_TEST_AGENT")
	if agent == "" {
		t.Skip("set ACP_BRIDGE_TEST_AGENT to opt into local initialize-only smoke test")
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- serve(ctx, ln, testToken, []string{agent, "acp", "--dangerously-skip-permissions"}, "", 1)
	}()
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("agent/server cleanup timed out")
		}
	}()
	conn, reader := connectTest(t, ln.Addr().String(), testToken)
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(8 * time.Second))
	if ack, err := reader.ReadString('\n'); err != nil || ack != "OK\n" {
		t.Fatal("agent authentication/start failed")
	}
	io.WriteString(conn, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":1,\"clientCapabilities\":{}}}\n")
	line, err := reader.ReadString('\n')
	var response struct {
		ID     int `json:"id"`
		Result struct {
			ProtocolVersion int `json:"protocolVersion"`
		} `json:"result"`
	}
	if err != nil || json.Unmarshal([]byte(line), &response) != nil || response.ID != 1 || response.Result.ProtocolVersion != 1 {
		t.Fatal("real agent did not return ACP v1 initialize response")
	}
	t.Log("real agent ACP v1 initialize succeeded through authenticated TCP")
}
