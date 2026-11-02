package protocol

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
)

// Encode fills Type and MsgID when empty, replaces nil slices and maps with
// empty ones (Pydantic rejects null for list fields that have defaults), and
// marshals the message. msg must be a pointer to one of the message structs.
func Encode(msgType string, msg any) ([]byte, error) {
	v := reflect.ValueOf(msg)
	if v.Kind() != reflect.Pointer || v.Elem().Kind() != reflect.Struct {
		return nil, fmt.Errorf("Encode: want pointer to struct, got %T", msg)
	}
	s := v.Elem()
	if f := s.FieldByName("Type"); f.IsValid() && f.Kind() == reflect.String && f.String() == "" {
		f.SetString(msgType)
	}
	if f := s.FieldByName("MsgID"); f.IsValid() && f.Kind() == reflect.String && f.String() == "" {
		f.SetString(NewID())
	}
	fillNil(s)
	return json.Marshal(msg)
}

func fillNil(v reflect.Value) {
	switch v.Kind() {
	case reflect.Struct:
		t := v.Type()
		for i := 0; i < v.NumField(); i++ {
			if !t.Field(i).IsExported() {
				continue
			}
			fillNil(v.Field(i))
		}
	case reflect.Slice:
		if v.IsNil() {
			if v.CanSet() {
				v.Set(reflect.MakeSlice(v.Type(), 0, 0))
			}
			return
		}
		for i := 0; i < v.Len(); i++ {
			fillNil(v.Index(i))
		}
	case reflect.Map:
		if v.IsNil() && v.CanSet() {
			v.Set(reflect.MakeMap(v.Type()))
		}
	case reflect.Pointer:
		if !v.IsNil() {
			fillNil(v.Elem())
		}
	}
}

// ErrUnknownKey is returned when the envelope names a key the agent did not pin.
var ErrUnknownKey = errors.New("envelope signed with an unknown key id")

// ErrBadSignature is returned when the signature does not verify.
var ErrBadSignature = errors.New("envelope signature does not verify")

// VerifyEnvelope checks the Ed25519 signature over the exact payload bytes,
// using only the keys pinned at enrollment, and returns those bytes. Callers
// must not parse the payload before this returns nil.
func VerifyEnvelope(env SignedEnvelope, keys map[string]ed25519.PublicKey) ([]byte, error) {
	pub, ok := keys[env.KeyID]
	if !ok {
		return nil, ErrUnknownKey
	}
	if len(pub) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("pinned key %q has wrong size", env.KeyID)
	}
	payload, err := base64.StdEncoding.DecodeString(env.PayloadB64)
	if err != nil {
		return nil, fmt.Errorf("payload_b64: %w", err)
	}
	sig, err := base64.StdEncoding.DecodeString(env.SignatureB64)
	if err != nil {
		return nil, fmt.Errorf("signature_b64: %w", err)
	}
	if len(sig) != ed25519.SignatureSize || !ed25519.Verify(pub, payload, sig) {
		return nil, ErrBadSignature
	}
	return payload, nil
}

// ParseSigningKeys decodes the pinned keys from enrollment.
func ParseSigningKeys(keys []SigningKey) (map[string]ed25519.PublicKey, error) {
	out := make(map[string]ed25519.PublicKey, len(keys))
	for _, k := range keys {
		raw, err := base64.StdEncoding.DecodeString(k.PublicKeyB64)
		if err != nil {
			return nil, fmt.Errorf("signing key %q: %w", k.KeyID, err)
		}
		if len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("signing key %q: want %d bytes, got %d", k.KeyID, ed25519.PublicKeySize, len(raw))
		}
		out[k.KeyID] = ed25519.PublicKey(raw)
	}
	return out, nil
}
