// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

// Package enroll handles the device identity: the Ed25519 key pair, the
// enrollment call, certificate renewal and the on-disk profile layout.
package enroll

import (
	"crypto/ed25519"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"time"

	"pool/agent/protocol"
)

// Profile file names inside <home>/profiles/<name>/.
const (
	keyFile     = "device.key"
	certFile    = "cert.pem"
	caFile      = "ca.pem"
	profileFile = "profile.json"
	PolicyFile  = "policy.yaml"
	StateFile   = "state.json"
	WorkDir     = "work"
	KillFile    = "KILL"
)

var profileNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,31}$`)

// Home returns the agent home directory: $POOL_AGENT_HOME, else
// ~/.config/pool-agent on Linux and %ProgramData%\pool-agent on Windows.
func Home() (string, error) {
	if h := os.Getenv("POOL_AGENT_HOME"); h != "" {
		return filepath.Abs(h)
	}
	if runtime.GOOS == "windows" {
		pd := os.Getenv("ProgramData")
		if pd == "" {
			return "", errors.New("ProgramData is not set; set POOL_AGENT_HOME")
		}
		return filepath.Join(pd, "pool-agent"), nil
	}
	cfg, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("cannot find config dir (set POOL_AGENT_HOME): %w", err)
	}
	return filepath.Join(cfg, "pool-agent"), nil
}

// ProfileDir returns <home>/profiles/<name> after validating the name.
func ProfileDir(home, name string) (string, error) {
	if !profileNameRE.MatchString(name) {
		return "", fmt.Errorf("invalid profile name %q (use lowercase letters, digits, - and _)", name)
	}
	return filepath.Join(home, "profiles", name), nil
}

// ListProfiles returns the names of enrolled profiles, sorted.
func ListProfiles(home string) ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(home, "profiles"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if e.IsDir() && profileNameRE.MatchString(e.Name()) {
			if _, err := os.Stat(filepath.Join(home, "profiles", e.Name(), profileFile)); err == nil {
				names = append(names, e.Name())
			}
		}
	}
	sort.Strings(names)
	return names, nil
}

// Profile is one enrollment with one server (plan §7.11). Nothing is shared
// between profiles.
type Profile struct {
	Name         string                `json:"name"`
	Server       string                `json:"server"`
	DeviceID     string                `json:"device_id"`
	WSPath       string                `json:"ws_path"`
	SigningKeys  []protocol.SigningKey `json:"signing_keys"`
	CertNotAfter time.Time             `json:"cert_not_after"`
	InsecureHTTP bool                  `json:"insecure_http"`
	ServerCAFile string                `json:"server_ca_file,omitempty"`
	EnrolledAt   time.Time             `json:"enrolled_at"`

	Dir     string             `json:"-"`
	Key     ed25519.PrivateKey `json:"-"`
	CertPEM []byte             `json:"-"`
	CAPEM   []byte             `json:"-"`
}

// Load reads a profile and its key and certificates from disk.
func Load(home, name string) (*Profile, error) {
	dir, err := ProfileDir(home, name)
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(filepath.Join(dir, profileFile))
	if err != nil {
		return nil, fmt.Errorf("profile %q is not enrolled: %w", name, err)
	}
	p := &Profile{}
	if err := json.Unmarshal(raw, p); err != nil {
		return nil, fmt.Errorf("profile %q: %w", name, err)
	}
	p.Dir = dir
	if p.Key, err = LoadKey(filepath.Join(dir, keyFile)); err != nil {
		return nil, err
	}
	if p.CertPEM, err = os.ReadFile(filepath.Join(dir, certFile)); err != nil {
		return nil, err
	}
	if p.CAPEM, err = os.ReadFile(filepath.Join(dir, caFile)); err != nil {
		return nil, err
	}
	if p.WSPath == "" {
		p.WSPath = protocol.DefaultWSPath
	}
	return p, nil
}

// Save writes profile.json, cert.pem and ca.pem (the key is written once at
// generation time by GenerateKey).
func (p *Profile) Save() error {
	if err := os.MkdirAll(p.Dir, 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileAtomic(filepath.Join(p.Dir, certFile), p.CertPEM, 0o600); err != nil {
		return err
	}
	if err := writeFileAtomic(filepath.Join(p.Dir, caFile), p.CAPEM, 0o600); err != nil {
		return err
	}
	return writeFileAtomic(filepath.Join(p.Dir, profileFile), append(raw, '\n'), 0o600)
}

// UpdateCert stores a renewed certificate after checking that it is for this
// device's key and was issued by the pinned CA.
func (p *Profile) UpdateCert(certPEM []byte) (time.Time, error) {
	cert, err := ParseCert(certPEM)
	if err != nil {
		return time.Time{}, err
	}
	if err := p.checkCert(cert); err != nil {
		return time.Time{}, err
	}
	p.CertPEM = certPEM
	p.CertNotAfter = cert.NotAfter
	return cert.NotAfter, p.Save()
}

func (p *Profile) checkCert(cert *x509.Certificate) error {
	pub, ok := cert.PublicKey.(ed25519.PublicKey)
	if !ok || !pub.Equal(p.Key.Public()) {
		return errors.New("certificate is not for this device's key")
	}
	ca, err := ParseCert(p.CAPEM)
	if err != nil {
		return fmt.Errorf("pinned CA: %w", err)
	}
	if err := cert.CheckSignatureFrom(ca); err != nil {
		return fmt.Errorf("certificate not issued by the pinned CA: %w", err)
	}
	if p.DeviceID != "" && cert.Subject.CommonName != p.DeviceID {
		return fmt.Errorf("certificate CN %q is not device id %q", cert.Subject.CommonName, p.DeviceID)
	}
	return nil
}

// SigningKeyMap returns the pinned server signing keys.
func (p *Profile) SigningKeyMap() (map[string]ed25519.PublicKey, error) {
	return protocol.ParseSigningKeys(p.SigningKeys)
}

// NeedsRenewal reports whether fewer than 8 hours of validity remain.
func (p *Profile) NeedsRenewal(now time.Time) bool {
	return p.CertNotAfter.Sub(now) < 8*time.Hour
}

// WSURL returns the WebSocket URL for this profile.
func (p *Profile) WSURL() (string, error) {
	u, err := url.Parse(p.Server)
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	case "http":
		u.Scheme = "ws"
	default:
		return "", fmt.Errorf("unsupported server scheme %q", u.Scheme)
	}
	u.Path = p.WSPath
	return u.String(), nil
}

// TLSConfig returns the client TLS config: system roots (or the configured
// server CA) and the device certificate as the client certificate, so a
// reverse proxy can enforce mTLS (design doc X1).
func (p *Profile) TLSConfig() (*tls.Config, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if p.ServerCAFile != "" {
		pool, err := loadCAPool(p.ServerCAFile)
		if err != nil {
			return nil, err
		}
		cfg.RootCAs = pool
	}
	if len(p.CertPEM) > 0 && p.Key != nil {
		block, _ := pem.Decode(p.CertPEM)
		if block != nil {
			cfg.Certificates = []tls.Certificate{{Certificate: [][]byte{block.Bytes}, PrivateKey: p.Key}}
		}
	}
	return cfg, nil
}

// UploadTLSConfig is for presigned upload URLs. They usually point at the
// server's own address (one public address, docs/design/easy-onboarding.md
// §4), which may use a private CA, but may also point at a public object
// store. So: the system roots plus the pinned server CA, and no device
// certificate (the store has no use for it).
func (p *Profile) UploadTLSConfig() (*tls.Config, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if p.ServerCAFile == "" {
		return cfg, nil
	}
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool() // e.g. Windows before Go 1.18 semantics, or no system store
	}
	raw, err := os.ReadFile(p.ServerCAFile)
	if err != nil {
		return nil, fmt.Errorf("server CA: %w", err)
	}
	if !pool.AppendCertsFromPEM(raw) {
		return nil, fmt.Errorf("server CA %s: no certificates found", p.ServerCAFile)
	}
	cfg.RootCAs = pool
	return cfg, nil
}

func loadCAPool(path string) (*x509.CertPool, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("server CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(raw) {
		return nil, fmt.Errorf("server CA %s: no certificates found", path)
	}
	return pool, nil
}

// ParseCert parses the first PEM certificate.
func ParseCert(pemBytes []byte) (*x509.Certificate, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil || block.Type != "CERTIFICATE" {
		return nil, errors.New("no PEM certificate found")
	}
	return x509.ParseCertificate(block.Bytes)
}

// CheckServerURL enforces https, except plain http to localhost when
// insecureHTTP is set (dev and tests only).
func CheckServerURL(server string, insecureHTTP bool) (string, error) {
	u, err := url.Parse(server)
	if err != nil {
		return "", fmt.Errorf("bad server URL: %w", err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("bad server URL %q", server)
	}
	switch u.Scheme {
	case "https":
	case "http":
		host := u.Hostname()
		if !insecureHTTP {
			return "", errors.New("server URL must use https (use --insecure-http only for localhost dev servers)")
		}
		if host != "localhost" && host != "127.0.0.1" && host != "::1" {
			return "", fmt.Errorf("--insecure-http is only allowed for localhost, not %q", host)
		}
	default:
		return "", fmt.Errorf("server URL scheme must be https, got %q", u.Scheme)
	}
	u.Path, u.RawQuery, u.Fragment = "", "", ""
	return u.String(), nil
}

func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if err := tmp.Chmod(perm); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}
