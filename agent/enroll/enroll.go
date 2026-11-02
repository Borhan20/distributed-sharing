// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

package enroll

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"pool/agent/protocol"
)

// GenerateKey creates a new Ed25519 key and writes it as PKCS#8 PEM with
// mode 0600 inside a 0700 directory. It refuses to overwrite an existing key.
//
// On Windows, file modes do not restrict access; the key file inherits the
// ACL of %ProgramData%\pool-agent. Restricting that ACL (or moving the key to
// OS-protected storage) is open work for Phase 0 (plan §8.6). UNTESTED.
func GenerateKey(path string) (ed25519.PrivateKey, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	if err := os.Chmod(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	der, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, fmt.Errorf("write key: %w", err)
	}
	defer f.Close()
	if err := pem.Encode(f, &pem.Block{Type: "PRIVATE KEY", Bytes: der}); err != nil {
		return nil, err
	}
	return priv, f.Close()
}

// LoadKey reads an Ed25519 PKCS#8 PEM key and refuses group/world-readable
// files on Unix.
func LoadKey(path string) (ed25519.PrivateKey, error) {
	if runtime.GOOS != "windows" {
		st, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		if st.Mode().Perm()&0o077 != 0 {
			return nil, fmt.Errorf("key %s has mode %v; it must be 0600", path, st.Mode().Perm())
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(raw)
	if block == nil || block.Type != "PRIVATE KEY" {
		return nil, fmt.Errorf("key %s: not a PKCS#8 PEM private key", path)
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	priv, ok := k.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("key %s is not Ed25519", path)
	}
	return priv, nil
}

// MakeCSR returns a PEM CSR signed by key with the given common name.
func MakeCSR(key ed25519.PrivateKey, commonName string) (string, error) {
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: commonName},
	}, key)
	if err != nil {
		return "", err
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der})), nil
}

// Options for Enroll.
type Options struct {
	Home         string
	Profile      string
	Server       string
	Code         string
	Name         string
	InsecureHTTP bool
	ServerCAFile string
	AgentVersion string
	HTTPClient   *http.Client
}

// Enroll registers this device with a server using a one-time code shown to
// the signed-in owner in the UI (plan §8.6) and writes the profile.
func Enroll(ctx context.Context, o Options) (*Profile, error) {
	server, err := CheckServerURL(o.Server, o.InsecureHTTP)
	if err != nil {
		return nil, err
	}
	dir, err := ProfileDir(o.Home, o.Profile)
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(filepath.Join(dir, profileFile)); err == nil {
		return nil, fmt.Errorf("profile %q is already enrolled (%s); choose another --profile", o.Profile, dir)
	}
	if err := os.MkdirAll(o.Home, 0o700); err != nil {
		return nil, err
	}
	keyPath := filepath.Join(dir, keyFile)
	var key ed25519.PrivateKey
	if _, err := os.Stat(keyPath); err == nil {
		// A previous enrollment attempt failed after creating the key; reuse it.
		if key, err = LoadKey(keyPath); err != nil {
			return nil, err
		}
	} else if key, err = GenerateKey(keyPath); err != nil {
		return nil, err
	}
	csr, err := MakeCSR(key, "pending")
	if err != nil {
		return nil, err
	}
	name := o.Name
	if name == "" {
		name = defaultDeviceName()
	}
	reqBody, err := json.Marshal(protocol.EnrollRequest{
		OneTimeCode: o.Code,
		CSRPEM:      csr,
		DeviceFacts: protocol.DeviceFacts{
			Name: name, OS: runtime.GOOS, Arch: runtime.GOARCH, AgentVersion: o.AgentVersion,
		},
	})
	if err != nil {
		return nil, err
	}

	p := &Profile{
		Name: o.Profile, Server: server, InsecureHTTP: o.InsecureHTTP, ServerCAFile: o.ServerCAFile,
		Dir: dir, Key: key,
	}
	client := o.HTTPClient
	if client == nil {
		tlsCfg, err := p.TLSConfig()
		if err != nil {
			return nil, err
		}
		client = &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: tlsCfg}}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, server+protocol.EnrollPath, bytes.NewReader(reqBody))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("enroll: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("enroll: server returned %s: %s", resp.Status, truncate(string(body), 300))
	}
	var er protocol.EnrollResponse
	if err := json.Unmarshal(body, &er); err != nil {
		return nil, fmt.Errorf("enroll: bad response: %w", err)
	}
	if er.DeviceID == "" || er.CertPEM == "" || er.CACertPEM == "" {
		return nil, errors.New("enroll: response is missing device_id or certificates")
	}
	if len(er.SigningKeys) == 0 {
		return nil, errors.New("enroll: response has no signing keys to pin")
	}
	if _, err := protocol.ParseSigningKeys(er.SigningKeys); err != nil {
		return nil, fmt.Errorf("enroll: %w", err)
	}
	p.DeviceID = er.DeviceID
	p.SigningKeys = er.SigningKeys
	p.CAPEM = []byte(er.CACertPEM)
	p.WSPath = er.WSPath
	if p.WSPath == "" {
		p.WSPath = protocol.DefaultWSPath
	}
	p.EnrolledAt = time.Now().UTC()
	cert, err := ParseCert([]byte(er.CertPEM))
	if err != nil {
		return nil, fmt.Errorf("enroll: certificate: %w", err)
	}
	if err := p.checkCert(cert); err != nil {
		return nil, fmt.Errorf("enroll: %w", err)
	}
	p.CertPEM = []byte(er.CertPEM)
	p.CertNotAfter = cert.NotAfter
	if err := p.Save(); err != nil {
		return nil, err
	}
	return p, nil
}

// SignHello signs the session challenge (design doc §3.2).
func SignHello(key ed25519.PrivateKey, nonceB64 string) (string, error) {
	nonce, err := base64.StdEncoding.DecodeString(nonceB64)
	if err != nil {
		return "", fmt.Errorf("challenge nonce: %w", err)
	}
	if len(nonce) < 16 {
		return "", errors.New("challenge nonce too short")
	}
	msg := append([]byte(protocol.HelloContext), nonce...)
	return base64.StdEncoding.EncodeToString(ed25519.Sign(key, msg)), nil
}

// defaultDeviceName avoids the hostname, which often contains a person's
// name; owners can pass --name to pick something meaningful.
func defaultDeviceName() string {
	var b [3]byte
	_, _ = rand.Read(b[:])
	return fmt.Sprintf("%s-%x", runtime.GOOS, b)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
