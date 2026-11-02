// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package enroll

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"pool/agent/protocol"
)

type fakeCA struct {
	key  ed25519.PrivateKey
	cert *x509.Certificate
	pem  []byte
}

func newFakeCA(t *testing.T) *fakeCA {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test device CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, pub, priv)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return &fakeCA{key: priv, cert: cert, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

func (ca *fakeCA) issue(t *testing.T, pub any, cn string, ttl time.Duration) []byte {
	t.Helper()
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: cn},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(ttl),
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, pub, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func enrollServer(t *testing.T, ca *fakeCA, mutate func(*protocol.EnrollResponse)) *httptest.Server {
	signPub, _, _ := ed25519.GenerateKey(rand.Reader)
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != protocol.EnrollPath || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		var req protocol.EnrollRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		if req.OneTimeCode != "ABCDE-FGHJK" {
			http.Error(w, `{"detail":"invalid or expired code"}`, 400)
			return
		}
		block, _ := pem.Decode([]byte(req.CSRPEM))
		csr, err := x509.ParseCertificateRequest(block.Bytes)
		if err != nil || csr.CheckSignature() != nil || csr.Subject.CommonName != "pending" {
			http.Error(w, "bad csr", 400)
			return
		}
		if _, ok := csr.PublicKey.(ed25519.PublicKey); !ok {
			http.Error(w, "not ed25519", 400)
			return
		}
		resp := protocol.EnrollResponse{
			DeviceID: "dev-123", CertPEM: string(ca.issue(t, csr.PublicKey, "dev-123", 24*time.Hour)),
			CACertPEM: string(ca.pem), CertNotAfter: time.Now().Add(24 * time.Hour),
			SigningKeys: []protocol.SigningKey{{KeyID: "k1", PublicKeyB64: base64.StdEncoding.EncodeToString(signPub)}},
			WSPath:      protocol.DefaultWSPath,
		}
		if mutate != nil {
			mutate(&resp)
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
}

func TestEnrollWritesProtectedProfile(t *testing.T) {
	ca := newFakeCA(t)
	srv := enrollServer(t, ca, nil)
	defer srv.Close()
	home := t.TempDir()
	p, err := Enroll(context.Background(), Options{
		Home: home, Profile: "default", Server: srv.URL, Code: "ABCDE-FGHJK", InsecureHTTP: true, AgentVersion: "test",
	})
	if err != nil {
		t.Fatalf("enroll: %v", err)
	}
	if p.DeviceID != "dev-123" || p.NeedsRenewal(time.Now()) {
		t.Fatalf("profile: %+v", p)
	}
	st, _ := os.Stat(filepath.Join(p.Dir, keyFile))
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("key mode %v", st.Mode().Perm())
	}
	dst, _ := os.Stat(p.Dir)
	if dst.Mode().Perm() != 0o700 {
		t.Fatalf("dir mode %v", dst.Mode().Perm())
	}
	loaded, err := Load(home, "default")
	if err != nil {
		t.Fatal(err)
	}
	if !loaded.Key.Equal(p.Key) || loaded.DeviceID != "dev-123" {
		t.Fatal("reloaded profile differs")
	}
	ws, _ := loaded.WSURL()
	if !strings.HasPrefix(ws, "ws://127.0.0.1") || !strings.HasSuffix(ws, protocol.DefaultWSPath) {
		t.Fatalf("ws url %s", ws)
	}
	names, _ := ListProfiles(home)
	if len(names) != 1 || names[0] != "default" {
		t.Fatalf("profiles %v", names)
	}
	// A second enrollment of the same profile is refused.
	if _, err := Enroll(context.Background(), Options{Home: home, Profile: "default", Server: srv.URL,
		Code: "ABCDE-FGHJK", InsecureHTTP: true}); err == nil {
		t.Fatal("re-enrolling an existing profile should fail")
	}

	// Renewal: a cert for our key from the pinned CA is accepted...
	newPEM := ca.issue(t, loaded.Key.Public(), "dev-123", 48*time.Hour)
	if _, err := loaded.UpdateCert(newPEM); err != nil {
		t.Fatalf("renew: %v", err)
	}
	// ...one for another key or from another CA is not.
	otherPub, _, _ := ed25519.GenerateKey(rand.Reader)
	if _, err := loaded.UpdateCert(ca.issue(t, otherPub, "dev-123", time.Hour)); err == nil {
		t.Fatal("cert for another key accepted")
	}
	other := newFakeCA(t)
	if _, err := loaded.UpdateCert(other.issue(t, loaded.Key.Public(), "dev-123", time.Hour)); err == nil {
		t.Fatal("cert from another CA accepted")
	}
	if _, err := loaded.UpdateCert(ca.issue(t, loaded.Key.Public(), "dev-999", time.Hour)); err == nil {
		t.Fatal("cert for another device id accepted")
	}
}

func TestEnrollRejectsBadResponses(t *testing.T) {
	ca := newFakeCA(t)
	cases := map[string]func(*protocol.EnrollResponse){
		"no signing keys": func(r *protocol.EnrollResponse) { r.SigningKeys = nil },
		"cert from other CA": func(r *protocol.EnrollResponse) {
			r.CACertPEM = string(newFakeCA(t).pem)
		},
		"short signing key": func(r *protocol.EnrollResponse) {
			r.SigningKeys[0].PublicKeyB64 = base64.StdEncoding.EncodeToString([]byte("short"))
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			srv := enrollServer(t, ca, mutate)
			defer srv.Close()
			_, err := Enroll(context.Background(), Options{Home: t.TempDir(), Profile: "default", Server: srv.URL,
				Code: "ABCDE-FGHJK", InsecureHTTP: true})
			if err == nil {
				t.Fatal("expected error")
			}
		})
	}
	srv := enrollServer(t, ca, nil)
	defer srv.Close()
	if _, err := Enroll(context.Background(), Options{Home: t.TempDir(), Profile: "default", Server: srv.URL,
		Code: "WRONG-CODE1", InsecureHTTP: true}); err == nil || !strings.Contains(err.Error(), "400") {
		t.Fatalf("wrong code: %v", err)
	}
}

func TestCheckServerURL(t *testing.T) {
	cases := []struct {
		url      string
		insecure bool
		ok       bool
	}{
		{"https://pool.example.org", false, true},
		{"https://pool.example.org/some/path", false, true},
		{"http://pool.example.org", false, false},
		{"http://pool.example.org", true, false},
		{"http://localhost:8000", false, false},
		{"http://localhost:8000", true, true},
		{"http://127.0.0.1:8000", true, true},
		{"ftp://pool.example.org", true, false},
		{"not a url", true, false},
	}
	for _, c := range cases {
		_, err := CheckServerURL(c.url, c.insecure)
		if (err == nil) != c.ok {
			t.Errorf("CheckServerURL(%q, %v) err=%v, want ok=%v", c.url, c.insecure, err, c.ok)
		}
	}
}

func TestLoadKeyRejectsLooseMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "k", "device.key")
	if _, err := GenerateKey(path); err != nil {
		t.Fatal(err)
	}
	if _, err := GenerateKey(path); err == nil {
		t.Fatal("GenerateKey overwrote an existing key")
	}
	_ = os.Chmod(path, 0o644)
	if _, err := LoadKey(path); err == nil {
		t.Fatal("world-readable key accepted")
	}
}

func TestSignHello(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	nonce := make([]byte, 32)
	_, _ = rand.Read(nonce)
	sigB64, err := SignHello(priv, base64.StdEncoding.EncodeToString(nonce))
	if err != nil {
		t.Fatal(err)
	}
	sig, _ := base64.StdEncoding.DecodeString(sigB64)
	if !ed25519.Verify(pub, append([]byte(protocol.HelloContext), nonce...), sig) {
		t.Fatal("hello signature does not verify with the context prefix")
	}
	if ed25519.Verify(pub, nonce, sig) {
		t.Fatal("signature must not verify over the bare nonce")
	}
}
