package session

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"pool/agent/enroll"
	"pool/agent/protocol"
	"pool/agent/sandbox"
)

func testOutput(t *testing.T, body []byte) sandbox.Output {
	t.Helper()
	path := filepath.Join(t.TempDir(), "result.txt")
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	return sandbox.Output{
		ArtifactRef: protocol.ArtifactRef{Path: "result.txt", SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body))},
		LocalPath:   path,
	}
}

type recorded struct {
	checksum string
	hasSum   bool
	body     []byte
}

func uploadServer(t *testing.T) (*httptest.Server, *recorded) {
	t.Helper()
	rec := &recorded{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, rec.hasSum = r.Header[http.CanonicalHeaderKey(checksumHeader)]
		rec.checksum = r.Header.Get(checksumHeader)
		rec.body, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return srv, rec
}

func TestPutSendsSignedChecksum(t *testing.T) {
	a := &Agent{cfg: Config{Profile: &enroll.Profile{InsecureHTTP: true}}}
	o := testOutput(t, []byte("real output\n"))
	srv, rec := uploadServer(t)
	raw := srv.URL + "/bucket/key?X-Amz-Algorithm=AWS4-HMAC-SHA256" +
		"&X-Amz-SignedHeaders=content-length%3Bhost%3Bx-amz-checksum-sha256&X-Amz-Signature=abc"
	if err := a.put(srv.Client(), raw, o); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("real output\n"))
	if want := base64.StdEncoding.EncodeToString(sum[:]); rec.checksum != want {
		t.Fatalf("checksum header = %q, want %q", rec.checksum, want)
	}
	if string(rec.body) != "real output\n" {
		t.Fatalf("body = %q", rec.body)
	}
}

func TestPutOmitsChecksumWhenNotSigned(t *testing.T) {
	// An unsigned extra x-amz-* header could make some stores reject the request.
	a := &Agent{cfg: Config{Profile: &enroll.Profile{InsecureHTTP: true}}}
	o := testOutput(t, []byte("data"))
	srv, rec := uploadServer(t)
	if err := a.put(srv.Client(), srv.URL+"/b/k?X-Amz-SignedHeaders=content-length%3Bhost", o); err != nil {
		t.Fatal(err)
	}
	if rec.hasSum {
		t.Fatalf("checksum header sent although the URL does not sign it")
	}
}

func TestPutRejectsBadHashAndPlainHTTP(t *testing.T) {
	srv, _ := uploadServer(t)
	signed := srv.URL + "/b/k?X-Amz-SignedHeaders=host%3Bx-amz-checksum-sha256"

	a := &Agent{cfg: Config{Profile: &enroll.Profile{InsecureHTTP: true}}}
	o := testOutput(t, []byte("data"))
	o.SHA256 = "not-hex"
	if err := a.put(srv.Client(), signed, o); err == nil {
		t.Fatal("expected an error for a malformed sha256")
	}

	secure := &Agent{cfg: Config{Profile: &enroll.Profile{InsecureHTTP: false}}}
	if err := secure.put(srv.Client(), signed, testOutput(t, []byte("data"))); err == nil {
		t.Fatal("expected plain http to be refused for a non-dev profile")
	}
}

// A server with a private CA (e.g. a LAN server behind Caddy's internal CA):
// uploads must trust the CA pinned at enrollment, as the WebSocket does.
func TestUploadTrustsPinnedServerCA(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	caFile := filepath.Join(t.TempDir(), "server-ca.pem")
	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if err := os.WriteFile(caFile, ca, 0o600); err != nil {
		t.Fatal(err)
	}
	o := testOutput(t, []byte("data"))

	unpinned := &enroll.Profile{}
	cfg, err := unpinned.UploadTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	a := &Agent{cfg: Config{Profile: unpinned}}
	if err := a.put(&http.Client{Transport: &http.Transport{TLSClientConfig: cfg}}, srv.URL+"/b/k", o); err == nil {
		t.Fatal("expected a TLS error without the pinned CA")
	}

	pinned := &enroll.Profile{ServerCAFile: caFile}
	cfg, err = pinned.UploadTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Certificates) != 0 {
		t.Fatal("uploads must not present the device certificate")
	}
	a = &Agent{cfg: Config{Profile: pinned}}
	if err := a.put(&http.Client{Transport: &http.Transport{TLSClientConfig: cfg}}, srv.URL+"/b/k", o); err != nil {
		t.Fatalf("upload with pinned CA: %v", err)
	}
}
