//go:build docker

package session

import (
	"crypto/sha256"
	"encoding/hex"
	"os/exec"
	"strings"
	"testing"
	"time"

	"pool/agent/protocol"
	"pool/agent/sandbox"
)

// TestRealContainerEndToEnd runs a signed offer through the session and the
// real Docker sandbox against the fake control plane.
func TestRealContainerEndToEnd(t *testing.T) {
	if err := exec.Command("docker", "version").Run(); err != nil {
		t.Skip("docker not available")
	}
	_ = exec.Command("docker", "pull", "-q", "busybox:1.36").Run()
	out, err := exec.Command("docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", "busybox:1.36").Output()
	if err != nil {
		t.Skip("busybox image not available")
	}
	image := strings.TrimSpace(string(out))
	rt := sandbox.NewLocalDocker(t.TempDir(), "default")
	h := startWith(t, rt)
	c := h.conn()
	o := h.fs.offer(h.prof.DeviceID, func(e *protocol.TaskEnvelope) {
		e.Spec.Image = image
		e.Spec.Command = []string{"sh", "-c", `echo "seed=$SEED attempt=$POOL_ATTEMPT_ID"; printf "seed=%s\n" "$SEED" > /out/result.txt`}
	}, nil)
	c.send(protocol.TypeOffer, o)
	if acc := c.expect("accept", 10*time.Second); acc["tier"] != "T1" {
		t.Fatalf("accept: %v", acc)
	}
	c.expect("started", 120*time.Second)
	prog := c.expect("progress", 30*time.Second)
	if !strings.Contains(prog["log_chunk"].(string), "seed=7 attempt="+o.AttemptID) {
		t.Fatalf("log: %v", prog)
	}
	ur := c.expect("upload_request", 60*time.Second)
	reason := "no_store"
	c.send(protocol.TypeUploadURLs, &protocol.UploadURLs{AttemptID: o.AttemptID, Reason: &reason})
	commit := c.expect("commit", 30*time.Second)
	sum := sha256.Sum256([]byte("seed=7\n"))
	outs := commit["outputs"].([]any)
	if commit["exit_code"].(float64) != 0 || len(outs) != 1 || outs[0].(map[string]any)["sha256"] != hex.EncodeToString(sum[:]) {
		t.Fatalf("commit: %v (upload_request %v)", commit, ur)
	}
	c.send(protocol.TypeCommitAck, &protocol.CommitAck{AttemptID: o.AttemptID, Accepted: true})
	waitFor(t, func() bool {
		out, _ := exec.Command("docker", "ps", "-aq", "--filter", "name="+sandbox.ContainerName(o.AttemptID)).Output()
		return len(strings.TrimSpace(string(out))) == 0
	}, "container removed")
}
