package protocol

// Contract tests (plan §10): the Go types must agree with the Pydantic models.
// They read the shared fixtures and generated JSON Schema from ../../protocol.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

const protocolDir = "../../protocol"

func readJSON(t *testing.T, path string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return m
}

func TestEveryFixtureRoundTrips(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(protocolDir, "fixtures", "messages", "*.json"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no fixtures found (err=%v)", err)
	}
	seen := map[string]bool{}
	for _, f := range files {
		name := strings.TrimSuffix(filepath.Base(f), ".json")
		t.Run(name, func(t *testing.T) {
			raw, _ := os.ReadFile(f)
			msg, err := Decode(raw)
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			seen[name] = true
			out, err := Encode(name, msg)
			if err != nil {
				t.Fatalf("encode: %v", err)
			}
			var want, got map[string]any
			_ = json.Unmarshal(raw, &want)
			_ = json.Unmarshal(out, &got)
			if got["type"] != name {
				t.Fatalf("type = %v, want %s", got["type"], name)
			}
			for k, v := range want {
				if v == nil {
					if g, ok := got[k]; ok && g != nil {
						t.Errorf("field %s: fixture null, got %v", k, g)
					}
					continue
				}
				if !reflect.DeepEqual(v, got[k]) {
					t.Errorf("field %s: fixture %v, round trip %v", k, v, got[k])
				}
			}
			for k := range got {
				if _, ok := want[k]; !ok {
					t.Errorf("Go emits field %q that the fixture does not have", k)
				}
			}
		})
	}
	for _, typ := range []string{
		TypeChallenge, TypeHello, TypeWelcome, TypeHeartbeat, TypeHeartbeatAck, TypeReady, TypeOffer,
		TypeNoWork, TypeAccept, TypeDecline, TypeStarted, TypeProgress, TypeUploadRequest, TypeUploadURLs,
		TypeCommit, TypeCommitAck, TypeAbort, TypeRenewCert, TypeCertRenewed, TypeCancel, TypePreempt,
		TypeRevoke, TypeTunnelOpen, TypeError,
	} {
		if !seen[typ] {
			t.Errorf("no fixture for message type %s", typ)
		}
	}
}

func TestTaskEnvelopeFixture(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join(protocolDir, "fixtures", "task_envelope.json"))
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var env TaskEnvelope
	if err := dec.Decode(&env); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if env.Epoch != 2 || env.Spec.Resources.GPUs != 1 || env.Spec.MinTier != T1 || env.Spec.Checkpoint == nil {
		t.Fatalf("unexpected envelope: %+v", env)
	}
	if env.Spec.Checkpoint.EverYS != 600 || env.Spec.Inputs[0].Mount != "/data" {
		t.Fatalf("unexpected spec: %+v", env.Spec)
	}
}

func TestSignatureVector(t *testing.T) {
	vec := readJSON(t, filepath.Join(protocolDir, "fixtures", "signed_envelope.json"))
	envMap := vec["envelope"].(map[string]any)
	env := SignedEnvelope{
		KeyID:        envMap["key_id"].(string),
		PayloadB64:   envMap["payload_b64"].(string),
		SignatureB64: envMap["signature_b64"].(string),
	}
	keys, err := ParseSigningKeys([]SigningKey{{KeyID: env.KeyID, PublicKeyB64: vec["public_key_b64"].(string)}})
	if err != nil {
		t.Fatal(err)
	}
	payload, err := VerifyEnvelope(env, keys)
	if err != nil {
		t.Fatalf("valid vector rejected: %v", err)
	}
	var te TaskEnvelope
	if err := json.Unmarshal(payload, &te); err != nil || te.Epoch != 2 {
		t.Fatalf("payload: %v %+v", err, te)
	}

	tampered := env
	tampered.PayloadB64 = vec["tampered_payload_b64"].(string)
	if _, err := VerifyEnvelope(tampered, keys); !errors.Is(err, ErrBadSignature) {
		t.Fatalf("tampered payload: err = %v, want ErrBadSignature", err)
	}
	unknown := env
	unknown.KeyID = "not-pinned"
	if _, err := VerifyEnvelope(unknown, keys); !errors.Is(err, ErrUnknownKey) {
		t.Fatalf("unknown key: err = %v", err)
	}
	sig, _ := base64.StdEncoding.DecodeString(env.SignatureB64)
	sig[0] ^= 1
	flipped := env
	flipped.SignatureB64 = base64.StdEncoding.EncodeToString(sig)
	if _, err := VerifyEnvelope(flipped, keys); !errors.Is(err, ErrBadSignature) {
		t.Fatalf("flipped signature: err = %v", err)
	}
}

// goTypes maps Pydantic model names to Go types.
var goTypes = map[string]reflect.Type{
	"Abort": reflect.TypeOf(Abort{}), "Accept": reflect.TypeOf(Accept{}),
	"ArtifactRef": reflect.TypeOf(ArtifactRef{}), "Capabilities": reflect.TypeOf(Capabilities{}),
	"Commit": reflect.TypeOf(Commit{}), "Decline": reflect.TypeOf(Decline{}), "GPUInfo": reflect.TypeOf(GPUInfo{}),
	"Heartbeat": reflect.TypeOf(Heartbeat{}), "Hello": reflect.TypeOf(Hello{}), "Progress": reflect.TypeOf(Progress{}),
	"Ready": reflect.TypeOf(Ready{}), "RenewCert": reflect.TypeOf(RenewCert{}), "Resources": reflect.TypeOf(Resources{}),
	"RunningAttempt": reflect.TypeOf(RunningAttempt{}), "ShareInfo": reflect.TypeOf(ShareInfo{}),
	"ShareLimits": reflect.TypeOf(ShareLimits{}), "Started": reflect.TypeOf(Started{}),
	"UploadRequest": reflect.TypeOf(UploadRequest{}), "Usage": reflect.TypeOf(Usage{}),
	"RunUsage":  reflect.TypeOf(RunUsage{}),
	"Challenge": reflect.TypeOf(Challenge{}), "Welcome": reflect.TypeOf(Welcome{}),
	"HeartbeatAck": reflect.TypeOf(HeartbeatAck{}), "Offer": reflect.TypeOf(Offer{}),
	"NoWork": reflect.TypeOf(NoWork{}), "UploadURLs": reflect.TypeOf(UploadURLs{}),
	"PullAuthRequest": reflect.TypeOf(PullAuthRequest{}),
	"PullAuth":        reflect.TypeOf(PullAuth{}),
	"UploadURL":       reflect.TypeOf(UploadURL{}), "CommitAck": reflect.TypeOf(CommitAck{}),
	"CertRenewed": reflect.TypeOf(CertRenewed{}), "Cancel": reflect.TypeOf(Cancel{}),
	"Preempt": reflect.TypeOf(Preempt{}), "Revoke": reflect.TypeOf(Revoke{}), "Error": reflect.TypeOf(Error{}),
	"TunnelOpen":     reflect.TypeOf(TunnelOpen{}),
	"SignedEnvelope": reflect.TypeOf(SignedEnvelope{}), "TaskEnvelope": reflect.TypeOf(TaskEnvelope{}),
	"TaskSpec": reflect.TypeOf(TaskSpec{}), "TaskResources": reflect.TypeOf(TaskResources{}),
	"TaskNetwork": reflect.TypeOf(TaskNetwork{}), "TaskCheckpoint": reflect.TypeOf(TaskCheckpoint{}),
	"TaskInput": reflect.TypeOf(TaskInput{}), "EnrollRequest": reflect.TypeOf(EnrollRequest{}),
	"EnrollResponse": reflect.TypeOf(EnrollResponse{}), "DeviceFacts": reflect.TypeOf(DeviceFacts{}),
	"SigningKey": reflect.TypeOf(SigningKey{}),
}

func jsonFields(t reflect.Type) []string {
	var out []string
	for i := 0; i < t.NumField(); i++ {
		tag := strings.Split(t.Field(i).Tag.Get("json"), ",")[0]
		if tag != "" && tag != "-" {
			out = append(out, tag)
		}
	}
	sort.Strings(out)
	return out
}

func schemaFields(def map[string]any) []string {
	props, _ := def["properties"].(map[string]any)
	out := make([]string, 0, len(props))
	for k := range props {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// TestGoTypesMatchJSONSchema checks that every Go struct has exactly the
// properties of the corresponding model in the generated JSON Schema.
func TestGoTypesMatchJSONSchema(t *testing.T) {
	checked := map[string]bool{}
	for _, file := range []string{"agent-message", "server-message", "task-envelope", "enroll-request", "enroll-response"} {
		schema := readJSON(t, filepath.Join(protocolDir, "schema", file+".schema.json"))
		defs, _ := schema["$defs"].(map[string]any)
		all := map[string]map[string]any{}
		for name, d := range defs {
			all[name] = d.(map[string]any)
		}
		if title, ok := schema["title"].(string); ok && schema["properties"] != nil {
			all[title] = schema
		}
		for name, def := range all {
			typ, ok := goTypes[name]
			if !ok {
				t.Errorf("%s: schema model %s has no Go type", file, name)
				continue
			}
			want, got := schemaFields(def), jsonFields(typ)
			if !reflect.DeepEqual(want, got) {
				t.Errorf("%s: %s fields differ\n schema: %v\n     go: %v", file, name, want, got)
			}
			checked[name] = true
		}
	}
	for name := range goTypes {
		if !checked[name] {
			t.Errorf("Go type %s not found in any schema", name)
		}
	}
}

// TestEncodeNeverSendsNullLists guards against Go nil slices becoming JSON
// null, which Pydantic rejects for list fields.
func TestEncodeNeverSendsNullLists(t *testing.T) {
	cases := map[string]any{
		TypeHello:         &Hello{},
		TypeHeartbeat:     &Heartbeat{},
		TypeReady:         &Ready{},
		TypeUploadRequest: &UploadRequest{},
		TypeCommit:        &Commit{},
	}
	for typ, msg := range cases {
		raw, err := Encode(typ, msg)
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		_ = json.Unmarshal(raw, &m)
		var walk func(path string, v any)
		walk = func(path string, v any) {
			switch x := v.(type) {
			case map[string]any:
				for k, vv := range x {
					walk(path+"."+k, vv)
				}
			case []any:
				for _, vv := range x {
					walk(path, vv)
				}
			}
		}
		walk(typ, m)
		for _, field := range []string{"running", "shares", "tiers", "artifacts", "outputs"} {
			if v, ok := m[field]; ok && v == nil {
				t.Errorf("%s.%s encoded as null", typ, field)
			}
		}
		if m["msg_id"] == "" || m["type"] != typ {
			t.Errorf("%s: type/msg_id not filled: %v", typ, m)
		}
		if typ == TypeHello {
			caps := m["capabilities"].(map[string]any)
			for _, f := range []string{"gpus", "tiers", "gpu_tiers"} {
				if caps[f] == nil {
					t.Errorf("hello.capabilities.%s encoded as null", f)
				}
			}
		}
	}
}
