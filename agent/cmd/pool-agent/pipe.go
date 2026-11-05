package main

// `pool-agent pipe 127.0.0.1:PORT` runs inside an app's container (pool-run step 2), started by the agent
// with `docker exec`. The app has no network; this pipe is how one tunnel connection reaches its port: bytes
// from stdin go to the port, bytes from the port go to stdout.

import (
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// pipeWait is how long pipe keeps trying while the app is still starting to listen.
const pipeWait = 10 * time.Second

// pipeLinger is how long pipe waits for the app's last bytes after the other side has gone. Without it, an app
// that never closes a connection would keep one pipe process per past connection in its container.
var pipeLinger = 5 * time.Second

func cmdPipe(args []string, in io.Reader, out io.Writer) error {
	if len(args) != 1 {
		return errors.New("usage: pool-agent pipe 127.0.0.1:PORT")
	}
	host, port, err := net.SplitHostPort(args[0])
	if err != nil || host != "127.0.0.1" {
		return errors.New("pipe connects only to 127.0.0.1:PORT, inside the app's own container")
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return fmt.Errorf("bad port %q", port)
	}
	var conn net.Conn
	deadline := time.Now().Add(pipeWait)
	for {
		conn, err = net.DialTimeout("tcp", args[0], 2*time.Second)
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			hint := ""
			if strings.Contains(err.Error(), "refused") {
				hint = " (is the app listening on this port, on 0.0.0.0 or 127.0.0.1?)"
			}
			return fmt.Errorf("cannot reach the app on port %s: %w%s", port, err, hint)
		}
		time.Sleep(200 * time.Millisecond)
	}
	defer conn.Close()
	go func() {
		_, _ = io.Copy(conn, in)
		if tc, ok := conn.(*net.TCPConn); ok {
			_ = tc.CloseWrite() // the caller finished sending: let the app see the end
		}
		// The caller is gone (the tunnel ended): give the app a moment to finish, then stop.
		_ = conn.SetReadDeadline(time.Now().Add(pipeLinger))
	}()
	_, err = io.Copy(out, conn)
	if errors.Is(err, os.ErrDeadlineExceeded) {
		return nil
	}
	return err
}
