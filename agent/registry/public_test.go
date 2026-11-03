package registry

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// A registry with token auth, like GHCR and Docker Hub: anonymous tokens can
// pull lab/public but not lab/private.
func fakeRegistry(t *testing.T) *httptest.Server {
	t.Helper()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/token":
			scope := r.URL.Query().Get("scope")
			tok := "anon-no-access"
			if scope == "repository:lab/public:pull" && r.Header.Get("Authorization") == "" {
				tok = "anon-public"
			}
			_ = json.NewEncoder(w).Encode(map[string]string{"token": tok})
		case strings.HasPrefix(r.URL.Path, "/v2/lab/"):
			auth := r.Header.Get("Authorization")
			if auth == "" {
				w.Header().Set("Www-Authenticate", `Bearer realm="`+srv.URL+`/token",service="fake"`)
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			if strings.HasPrefix(r.URL.Path, "/v2/lab/public/") && auth == "Bearer anon-public" {
				w.WriteHeader(http.StatusOK)
				return
			}
			w.WriteHeader(http.StatusUnauthorized)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestCheckPublic(t *testing.T) {
	srv := fakeRegistry(t)
	host := strings.TrimPrefix(srv.URL, "http://") // 127.0.0.1:port: plain http is allowed for loopback only
	digest := "@sha256:" + strings.Repeat("c", 64)
	if err := CheckPublic(context.Background(), srv.Client(), host+"/lab/public"+digest); err != nil {
		t.Fatalf("public image: %v", err)
	}
	err := CheckPublic(context.Background(), srv.Client(), host+"/lab/private"+digest)
	if !errors.Is(err, ErrPrivate) {
		t.Fatalf("private image: got %v, want ErrPrivate", err)
	}
	srv.Close()
	if err := CheckPublic(context.Background(), nil, host+"/lab/public"+digest); err == nil {
		t.Fatal("an unreachable registry must not count as public")
	}
}

func TestParse(t *testing.T) {
	d := "sha256:" + strings.Repeat("a", 64)
	for _, c := range []struct{ in, base, repo, ref string }{
		{"busybox@" + d, "https://registry-1.docker.io", "library/busybox", d},
		{"docker.io/lab/app:1@" + d, "https://registry-1.docker.io", "lab/app", d},
		{"ghcr.io/lab/models/train@" + d, "https://ghcr.io", "lab/models/train", d},
		{"127.0.0.1:5000/lab/app@" + d, "http://127.0.0.1:5000", "lab/app", d},
		{"registry.example.org:5000/x/y:2", "https://registry.example.org:5000", "x/y", "2"},
	} {
		got, err := Parse(c.in)
		if err != nil || got.Base != c.base || got.Repository != c.repo || got.Reference != c.ref {
			t.Errorf("Parse(%q) = %+v, %v", c.in, got, err)
		}
	}
}
