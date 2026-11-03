// SECURITY-SENSITIVE: keeps one group's private images from another group. Needs security review (plan §8.10).

// Package registry checks, without any credentials, whether an image can be
// pulled from its registry.
//
// Why not "docker pull": Docker (with the containerd image store) does not ask
// the registry when the image's content is already on this computer, so a pull
// cannot tell a public image from a private one that another group pulled here
// with its own token. This asks the registry itself, as an anonymous client:
// GET the manifest; on 401 get an anonymous token from the realm the registry
// names, and try once more.
package registry

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var paramRE = regexp.MustCompile(`(\w+)="([^"]*)"`)

// Ref is an image name split into what the registry API needs.
type Ref struct {
	Base       string // https://ghcr.io, https://registry-1.docker.io, http://127.0.0.1:5000
	Repository string // lab/app, library/busybox
	Reference  string // sha256:... (or a tag)
}

func isLoopback(host string) bool {
	h, _, err := net.SplitHostPort(host)
	if err != nil {
		h = host
	}
	if h == "localhost" {
		return true
	}
	ip := net.ParseIP(h)
	return ip != nil && ip.IsLoopback()
}

// Parse follows Docker's naming rules (and pool's server, control-plane/pool_cp/registry.py).
func Parse(image string) (Ref, error) {
	name, ref := image, "latest"
	if i := strings.Index(image, "@"); i >= 0 {
		name, ref = image[:i], image[i+1:]
	}
	host, path := "docker.io", name
	if i := strings.Index(name, "/"); i >= 0 {
		first := name[:i]
		if strings.ContainsAny(first, ".:") || first == "localhost" {
			host, path = strings.ToLower(first), name[i+1:]
		}
	}
	if j := strings.LastIndex(path, "/"); strings.Contains(path[j+1:], ":") {
		tag := path[j+1:]
		if ref == "latest" {
			ref = tag[strings.Index(tag, ":")+1:]
		}
		path = path[:j+1] + tag[:strings.Index(tag, ":")]
	}
	switch host {
	case "docker.io", "index.docker.io", "registry-1.docker.io":
		if !strings.Contains(path, "/") {
			path = "library/" + path
		}
		return Ref{Base: "https://registry-1.docker.io", Repository: path, Reference: ref}, nil
	}
	if path == "" {
		return Ref{}, fmt.Errorf("bad image name %q", image)
	}
	scheme := "https"
	if isLoopback(host) {
		scheme = "http" // only for a registry on this same computer
	}
	return Ref{Base: scheme + "://" + host, Repository: path, Reference: ref}, nil
}

var manifestTypes = strings.Join([]string{
	"application/vnd.oci.image.index.v1+json",
	"application/vnd.oci.image.manifest.v1+json",
	"application/vnd.docker.distribution.manifest.list.v2+json",
	"application/vnd.docker.distribution.manifest.v2+json",
}, ", ")

// ErrPrivate means the registry refuses this image to anonymous clients.
var ErrPrivate = errors.New("the registry refuses this image without credentials")

// CheckPublic returns nil if anyone can pull the image, ErrPrivate (wrapped)
// if the registry refuses it without credentials, or another error if the
// registry could not be asked (callers treat that as "not public").
func CheckPublic(ctx context.Context, client *http.Client, image string) error {
	ref, err := Parse(image)
	if err != nil {
		return err
	}
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{Proxy: http.ProxyFromEnvironment}}
	}
	manifest := ref.Base + "/v2/" + ref.Repository + "/manifests/" + ref.Reference
	status, header, err := head(ctx, client, manifest, "")
	if err != nil {
		return err
	}
	if status == http.StatusUnauthorized && strings.HasPrefix(strings.ToLower(header), "bearer ") {
		token, err := anonymousToken(ctx, client, header, ref.Repository)
		if err != nil {
			return err
		}
		if status, _, err = head(ctx, client, manifest, token); err != nil {
			return err
		}
	}
	switch status {
	case http.StatusOK:
		return nil
	case http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound:
		return fmt.Errorf("%w (HTTP %d)", ErrPrivate, status)
	default:
		return fmt.Errorf("registry answered HTTP %d", status)
	}
}

func head(ctx context.Context, client *http.Client, u, token string) (int, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, u, nil)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("Accept", manifestTypes)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, "", fmt.Errorf("cannot reach the registry: %w", err)
	}
	resp.Body.Close()
	return resp.StatusCode, resp.Header.Get("Www-Authenticate"), nil
}

func anonymousToken(ctx context.Context, client *http.Client, challenge, repository string) (string, error) {
	params := map[string]string{}
	for _, m := range paramRE.FindAllStringSubmatch(challenge, -1) {
		params[m[1]] = m[2]
	}
	realm, err := url.Parse(params["realm"])
	if err != nil || realm.Host == "" || (realm.Scheme != "https" && !(realm.Scheme == "http" && isLoopback(realm.Host))) {
		return "", errors.New("the registry names no usable token service")
	}
	q := realm.Query()
	if params["service"] != "" {
		q.Set("service", params["service"])
	}
	q.Set("scope", "repository:"+repository+":pull")
	realm.RawQuery = q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, realm.String(), nil)
	if err != nil {
		return "", err
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("cannot reach the registry's token service: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%w (token service HTTP %d)", ErrPrivate, resp.StatusCode)
	}
	var body struct {
		Token       string `json:"token"`
		AccessToken string `json:"access_token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", errors.New("unreadable answer from the registry's token service")
	}
	if body.Token != "" {
		return body.Token, nil
	}
	return body.AccessToken, nil
}
