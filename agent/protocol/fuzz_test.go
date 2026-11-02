// SECURITY-SENSITIVE: needs security review before merge (plan §8.10).
//
// Fuzz targets for the protocol parsers (plan §9 Phase 5: Go native fuzzing).
// Seeds come from the shared contract fixtures. Run longer locally with:
//
//	go test ./protocol -run '^$' -fuzz '^FuzzDecode$' -fuzztime 2m
package protocol

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func seedFixtures(f *testing.F) {
	files, _ := filepath.Glob(filepath.Join(protocolDir, "fixtures", "messages", "*.json"))
	for _, p := range files {
		if b, err := os.ReadFile(p); err == nil {
			f.Add(b)
		}
	}
	f.Add([]byte(`{"type":"offer"}`))
	f.Add([]byte(`{"type":"heartbeat_ack","renewed":null,"stale":[1]}`))
	f.Add([]byte(`null`))
}

// FuzzDecode: any server frame must either decode or return an error, never panic,
// and a decoded message must re-encode.
func FuzzDecode(f *testing.F) {
	seedFixtures(f)
	f.Fuzz(func(t *testing.T, raw []byte) {
		msg, err := Decode(raw)
		if err != nil {
			return
		}
		var env Envelope
		if json.Unmarshal(raw, &env) != nil {
			t.Fatalf("decoded a frame whose envelope does not parse")
		}
		if _, err := Encode(env.Type, msg); err != nil {
			t.Fatalf("decoded %s but cannot re-encode: %v", env.Type, err)
		}
	})
}

// FuzzVerifyEnvelope: only a payload signed by the pinned key may verify.
func FuzzVerifyEnvelope(f *testing.F) {
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		f.Fatal(err)
	}
	keys := map[string]ed25519.PublicKey{"k1": pub}
	good := []byte(`{"v":"v1alpha1","epoch":1}`)
	f.Add(good, ed25519.Sign(priv, good), "k1")
	f.Add([]byte("x"), []byte("short"), "k1")
	f.Add(good, ed25519.Sign(priv, good), "unknown")
	f.Fuzz(func(t *testing.T, payload, sig []byte, keyID string) {
		env := SignedEnvelope{KeyID: keyID, PayloadB64: base64.StdEncoding.EncodeToString(payload),
			SignatureB64: base64.StdEncoding.EncodeToString(sig)}
		out, err := VerifyEnvelope(env, keys)
		if err != nil {
			return
		}
		if keyID != "k1" || !ed25519.Verify(pub, payload, sig) || string(out) != string(payload) {
			t.Fatalf("envelope verified without a valid signature from the pinned key")
		}
	})
}
