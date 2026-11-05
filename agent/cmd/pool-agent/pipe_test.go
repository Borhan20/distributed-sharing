package main

import (
	"bytes"
	"io"
	"net"
	"strings"
	"testing"
)

func TestPipeCopiesBothWays(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		got, _ := io.ReadAll(c) // until the pipe closes its writing side
		_, _ = c.Write(append([]byte("echo:"), got...))
		_ = c.Close()
	}()
	var out bytes.Buffer
	if err := cmdPipe([]string{ln.Addr().String()}, strings.NewReader("hello"), &out); err != nil {
		t.Fatal(err)
	}
	if out.String() != "echo:hello" {
		t.Fatalf("got %q", out.String())
	}
}

func TestPipeOnlyReachesLoopback(t *testing.T) {
	for _, addr := range []string{"10.0.0.1:80", "localhost:80", "127.0.0.1:0", "127.0.0.1:99999", "x"} {
		if err := cmdPipe([]string{addr}, strings.NewReader(""), io.Discard); err == nil {
			t.Errorf("%s accepted", addr)
		}
	}
}
