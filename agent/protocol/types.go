// Package protocol holds hand-written Go types for the pool agent protocol
// v1alpha1. The Pydantic models in protocol/pool_protocol/messages.py are the
// source of truth; contract_test.go checks these types against the shared
// fixtures in protocol/fixtures (plan §10, design doc deviation X2).
//
// SECURITY-SENSITIVE: changes here need a security reviewer (plan §8.10).
package protocol

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"time"
)

const (
	Version            = "v1alpha1"
	HelloContext       = "pool-agent-hello-v1:"
	MaxLogChunkBytes   = 64 * 1024
	DefaultWSPath      = "/api/v1/agent/ws"
	EnrollPath         = "/api/v1/agent/enroll"
	AcceptAllMembers   = "all-members"
	DefaultOutputsPath = "/out"
)

// Tier is an isolation tier, T0 (weakest) to T3 (strongest).
type Tier string

const (
	T0 Tier = "T0"
	T1 Tier = "T1"
	T2 Tier = "T2"
	T3 Tier = "T3"
)

// Rank returns 0..3, or -1 for an unknown tier.
func (t Tier) Rank() int {
	switch t {
	case T0:
		return 0
	case T1:
		return 1
	case T2:
		return 2
	case T3:
		return 3
	}
	return -1
}

// Decline reasons (design doc §3.3).
const (
	DeclineSignatureInvalid      = "signature_invalid"
	DeclineEnvelopeExpired       = "envelope_expired"
	DeclineWrongDevice           = "wrong_device"
	DeclineGroupNotShared        = "group_not_shared"
	DeclineOutsideWindow         = "outside_window"
	DeclineOwnerActive           = "owner_active"
	DeclineSubmitterNotAccepted  = "submitter_not_accepted"
	DeclineProbationNotAccepted  = "probation_not_accepted"
	DeclineTierNotAllowed        = "tier_not_allowed"
	DeclineResourcesExceedLimits = "resources_exceed_limits"
	DeclineEgressNotAllowed      = "egress_not_allowed"
	DeclineImageNotAllowed       = "image_not_allowed"
	DeclineAppsNotAllowed        = "apps_not_allowed"
	DeclineKillSwitch            = "kill_switch"
	DeclineBusy                  = "busy"
	DeclineOther                 = "other"
)

// Abort reasons.
const (
	AbortOwnerReturned = "owner_returned"
	AbortKillSwitch    = "kill_switch"
	AbortPolicy        = "policy"
	AbortAgentShutdown = "agent_shutdown"
	AbortDiskFull      = "disk_full"
	AbortRuntimeError  = "runtime_error"
)

// NewID returns a random UUIDv4 string.
func NewID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// ---------------------------------------------------------------------------
// Shared value types
// ---------------------------------------------------------------------------

type Resources struct {
	CPU      float64 `json:"cpu"`
	MemoryMB int64   `json:"memory_mb"`
	DiskMB   int64   `json:"disk_mb"`
	GPUs     int     `json:"gpus"`
}

type GPUInfo struct {
	Vendor   string `json:"vendor"`
	Model    string `json:"model"`
	MemoryMB int64  `json:"memory_mb"`
}

type Capabilities struct {
	OS       string    `json:"os"`
	Arch     string    `json:"arch"`
	CPUCores int       `json:"cpu_cores"`
	MemoryMB int64     `json:"memory_mb"`
	DiskMB   int64     `json:"disk_mb"`
	GPUs     []GPUInfo `json:"gpus"`
	Tiers    []Tier    `json:"tiers"`
	GPUTiers []Tier    `json:"gpu_tiers"`
	AlwaysOn bool      `json:"always_on"`
}

type ShareLimits struct {
	CPUPercent int   `json:"cpu_percent"`
	MemoryMB   int64 `json:"memory_mb"`
	DiskMB     int64 `json:"disk_mb"`
	GPU        bool  `json:"gpu"`
}

// AcceptFrom is either "all-members" or a list of user handles.
type AcceptFrom struct {
	All     bool
	Handles []string
}

func (a AcceptFrom) MarshalJSON() ([]byte, error) {
	if a.All {
		return json.Marshal(AcceptAllMembers)
	}
	h := a.Handles
	if h == nil {
		h = []string{}
	}
	return json.Marshal(h)
}

func (a *AcceptFrom) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err == nil {
		if s != AcceptAllMembers {
			return fmt.Errorf("accept_from: unknown value %q", s)
		}
		*a = AcceptFrom{All: true}
		return nil
	}
	var list []string
	if err := json.Unmarshal(b, &list); err != nil {
		return fmt.Errorf("accept_from: want %q or a list of handles", AcceptAllMembers)
	}
	*a = AcceptFrom{Handles: list}
	return nil
}

// Accepts reports whether a submitter handle is accepted.
func (a AcceptFrom) Accepts(handle string) bool {
	if a.All {
		return true
	}
	for _, h := range a.Handles {
		if h == handle {
			return true
		}
	}
	return false
}

type ShareInfo struct {
	Group            string      `json:"group"`
	Limits           ShareLimits `json:"limits"`
	AcceptFrom       AcceptFrom  `json:"accept_from"`
	AcceptNewMembers bool        `json:"accept_new_members"`
	ActiveNow        bool        `json:"active_now"`
	AllowApps        bool        `json:"allow_apps"`
	AppHours         float64     `json:"app_hours"`
}

type RunningAttempt struct {
	AttemptID string `json:"attempt_id"`
	Epoch     int64  `json:"epoch"`
	State     string `json:"state"` // assigned | running
}

type Usage struct {
	CPUPercent float64 `json:"cpu_percent"`
	MemoryMB   int64   `json:"memory_mb"`
}

type SignedEnvelope struct {
	KeyID        string `json:"key_id"`
	PayloadB64   string `json:"payload_b64"`
	SignatureB64 string `json:"signature_b64"`
}

type ArtifactRef struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

type UploadURL struct {
	Path      string    `json:"path"`
	SHA256    string    `json:"sha256"`
	URL       string    `json:"url"`
	ExpiresAt time.Time `json:"expires_at"`
}

// ---------------------------------------------------------------------------
// Task envelope
// ---------------------------------------------------------------------------

type TaskResources struct {
	CPU         float64 `json:"cpu"`
	MemoryMB    int64   `json:"memory_mb"`
	DiskMB      int64   `json:"disk_mb"`
	GPUs        int     `json:"gpus"`
	GPUVendor   *string `json:"gpu_vendor"`
	GPUMemoryMB int64   `json:"gpu_memory_mb"`
}

type TaskNetwork struct {
	Egress string   `json:"egress"` // none | allowlist
	Allow  []string `json:"allow"`
}

type TaskCheckpoint struct {
	Path   string `json:"path"`
	EverYS int64  `json:"every_s"`
}

type TaskInput struct {
	Ref   string  `json:"ref"`
	Mount string  `json:"mount"`
	URL   *string `json:"url"`
}

type TaskSpec struct {
	Image       string            `json:"image"`
	Command     []string          `json:"command"`
	Args        []string          `json:"args"`
	Workdir     *string           `json:"workdir"`
	Env         map[string]string `json:"env"`
	TaskIndex   int               `json:"task_index"`
	TaskCount   int               `json:"task_count"`
	Resources   TaskResources     `json:"resources"`
	MinTier     Tier              `json:"min_tier"`
	Network     TaskNetwork       `json:"network"`
	DataClass   string            `json:"data_class"`
	MaxRuntimeS int64             `json:"max_runtime_s"`
	OutputsPath string            `json:"outputs_path"`
	Checkpoint  *TaskCheckpoint   `json:"checkpoint"`
	Inputs      []TaskInput       `json:"inputs"`
	// An app (pool-run step 2): ports it listens on inside its container. The container has no network;
	// connections come only through TunnelOpen.
	Ports []int `json:"ports,omitempty"`
}

type TaskEnvelope struct {
	V                  string    `json:"v"`
	AttemptID          string    `json:"attempt_id"`
	TaskID             string    `json:"task_id"`
	JobID              string    `json:"job_id"`
	Epoch              int64     `json:"epoch"`
	Group              string    `json:"group"`
	GroupID            string    `json:"group_id"`
	Submitter          string    `json:"submitter"`
	SubmitterProbation bool      `json:"submitter_probation"`
	DeviceID           string    `json:"device_id"`
	IssuedAt           time.Time `json:"issued_at"`
	ExpiresAt          time.Time `json:"expires_at"`
	Spec               TaskSpec  `json:"spec"`
	// RegistryAuth: the image is private; ask for a short-lived pull token
	// (pull_auth_request) before pulling it.
	RegistryAuth bool `json:"registry_auth"`
}

// ---------------------------------------------------------------------------
// Enrollment (HTTPS)
// ---------------------------------------------------------------------------

type DeviceFacts struct {
	Name         string `json:"name"`
	OS           string `json:"os"`
	Arch         string `json:"arch"`
	AgentVersion string `json:"agent_version"`
}

type EnrollRequest struct {
	OneTimeCode string      `json:"one_time_code"`
	CSRPEM      string      `json:"csr_pem"`
	DeviceFacts DeviceFacts `json:"device_facts"`
}

type SigningKey struct {
	KeyID        string `json:"key_id"`
	PublicKeyB64 string `json:"public_key_b64"`
}

type EnrollResponse struct {
	DeviceID     string       `json:"device_id"`
	CertPEM      string       `json:"cert_pem"`
	CACertPEM    string       `json:"ca_cert_pem"`
	CertNotAfter time.Time    `json:"cert_not_after"`
	SigningKeys  []SigningKey `json:"signing_keys"`
	WSPath       string       `json:"ws_path"`
}

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

// Envelope is used to peek at the "type" of an incoming frame.
type Envelope struct {
	Type  string `json:"type"`
	MsgID string `json:"msg_id"`
}

type Challenge struct {
	Type       string    `json:"type"`
	MsgID      string    `json:"msg_id"`
	NonceB64   string    `json:"nonce_b64"`
	ServerTime time.Time `json:"server_time"`
}

type Hello struct {
	Type              string           `json:"type"`
	MsgID             string           `json:"msg_id"`
	Protocol          string           `json:"protocol"`
	AgentVersion      string           `json:"agent_version"`
	DeviceID          string           `json:"device_id"`
	Profile           string           `json:"profile"`
	CertPEM           string           `json:"cert_pem"`
	NonceSignatureB64 string           `json:"nonce_signature_b64"`
	Capabilities      Capabilities     `json:"capabilities"`
	Shares            []ShareInfo      `json:"shares"`
	PolicyDigest      string           `json:"policy_digest"`
	Running           []RunningAttempt `json:"running"`
}

type Welcome struct {
	Type               string    `json:"type"`
	MsgID              string    `json:"msg_id"`
	DeviceID           string    `json:"device_id"`
	HeartbeatIntervalS int       `json:"heartbeat_interval_s"`
	LeaseTTLS          int       `json:"lease_ttl_s"`
	ServerTime         time.Time `json:"server_time"`
	Stale              []string  `json:"stale"`
}

type Heartbeat struct {
	Type        string           `json:"type"`
	MsgID       string           `json:"msg_id"`
	Running     []RunningAttempt `json:"running"`
	Usage       Usage            `json:"usage"`
	OwnerActive bool             `json:"owner_active"`
	Shares      []ShareInfo      `json:"shares"`
}

type HeartbeatAck struct {
	Type           string    `json:"type"`
	MsgID          string    `json:"msg_id"`
	Renewed        []string  `json:"renewed"`
	Stale          []string  `json:"stale"`
	LeaseExpiresAt time.Time `json:"lease_expires_at"`
}

type Ready struct {
	Type  string    `json:"type"`
	MsgID string    `json:"msg_id"`
	Free  Resources `json:"free"`
	Tiers []Tier    `json:"tiers"`
	// Digests of images this computer ran recently and still has: the server prefers tasks that use them, so
	// they start without a download (docs/design/pool-decides.md §4.4).
	Images []string `json:"images,omitempty"`
}

type Offer struct {
	Type      string         `json:"type"`
	MsgID     string         `json:"msg_id"`
	AttemptID string         `json:"attempt_id"`
	TaskID    string         `json:"task_id"`
	Epoch     int64          `json:"epoch"`
	ExpiresAt time.Time      `json:"expires_at"`
	Envelope  SignedEnvelope `json:"envelope"`
}

type NoWork struct {
	Type        string `json:"type"`
	MsgID       string `json:"msg_id"`
	RetryAfterS int    `json:"retry_after_s"`
}

type Accept struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Epoch     int64  `json:"epoch"`
	Tier      Tier   `json:"tier"`
}

type Decline struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Epoch     int64  `json:"epoch"`
	Reason    string `json:"reason"`
	Detail    string `json:"detail"`
}

type Started struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Epoch     int64  `json:"epoch"`
}

type Progress struct {
	Type          string  `json:"type"`
	MsgID         string  `json:"msg_id"`
	AttemptID     string  `json:"attempt_id"`
	Epoch         int64   `json:"epoch"`
	Seq           int64   `json:"seq"`
	Stream        string  `json:"stream"` // stdout | stderr | agent
	LogChunk      string  `json:"log_chunk"`
	CheckpointRef *string `json:"checkpoint_ref"`
}

type UploadRequest struct {
	Type      string        `json:"type"`
	MsgID     string        `json:"msg_id"`
	AttemptID string        `json:"attempt_id"`
	Epoch     int64         `json:"epoch"`
	Artifacts []ArtifactRef `json:"artifacts"`
}

type UploadURLs struct {
	Type      string      `json:"type"`
	MsgID     string      `json:"msg_id"`
	AttemptID string      `json:"attempt_id"`
	URLs      []UploadURL `json:"urls"`
	Reason    *string     `json:"reason"`
}

// PullAuthRequest asks for a short-lived token to pull this attempt's private image.
type PullAuthRequest struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Epoch     int64  `json:"epoch"`
}

// PullAuth carries a bearer token for one image (pull only). Registry is the
// key for Docker's config "auths". Without a token, Reason says why.
type PullAuth struct {
	Type      string     `json:"type"`
	MsgID     string     `json:"msg_id"`
	AttemptID string     `json:"attempt_id"`
	Registry  *string    `json:"registry"`
	Token     *string    `json:"token"`
	ExpiresAt *time.Time `json:"expires_at"`
	Reason    *string    `json:"reason"`
}

type Commit struct {
	Type           string        `json:"type"`
	MsgID          string        `json:"msg_id"`
	IdempotencyKey string        `json:"idempotency_key"`
	TaskID         string        `json:"task_id"`
	AttemptID      string        `json:"attempt_id"`
	Epoch          int64         `json:"epoch"`
	ExitCode       int           `json:"exit_code"`
	Outputs        []ArtifactRef `json:"outputs"`
	Error          *string       `json:"error"`
	Usage          *RunUsage     `json:"usage,omitempty"`
}

// RunUsage is what one attempt used, as `docker stats` measured it (docs/design/pool-decides.md §4.2).
// The server treats it as the device's claim: it caps every number at what the task was given.
type RunUsage struct {
	PeakMemoryMB int64   `json:"peak_memory_mb"`
	CPUSeconds   float64 `json:"cpu_seconds"`
	PeakCPU      float64 `json:"peak_cpu"`
	WallSeconds  float64 `json:"wall_seconds"`
	OutputBytes  int64   `json:"output_bytes"`
	ImageMB      int64   `json:"image_mb"`
	OOMKilled    bool    `json:"oom_killed"`
	Samples      int     `json:"samples"`
}

type CommitAck struct {
	Type      string  `json:"type"`
	MsgID     string  `json:"msg_id"`
	AttemptID string  `json:"attempt_id"`
	Accepted  bool    `json:"accepted"`
	Reason    *string `json:"reason"`
}

type Abort struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Epoch     int64  `json:"epoch"`
	Reason    string `json:"reason"`
	Detail    string `json:"detail"`
}

type RenewCert struct {
	Type   string `json:"type"`
	MsgID  string `json:"msg_id"`
	CSRPEM string `json:"csr_pem"`
}

type CertRenewed struct {
	Type         string    `json:"type"`
	MsgID        string    `json:"msg_id"`
	CertPEM      string    `json:"cert_pem"`
	CertNotAfter time.Time `json:"cert_not_after"`
}

type Cancel struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Reason    string `json:"reason"`
}

type Preempt struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	AttemptID string `json:"attempt_id"`
	Reason    string `json:"reason"`
}

// TunnelOpen asks the agent to connect one tunnel to an app's port (pool-run step 2).
type TunnelOpen struct {
	Type      string `json:"type"`
	MsgID     string `json:"msg_id"`
	TunnelID  string `json:"tunnel_id"`
	AttemptID string `json:"attempt_id"`
	Port      int    `json:"port"`
	Secret    string `json:"secret"`
}

type Revoke struct {
	Type   string `json:"type"`
	MsgID  string `json:"msg_id"`
	Group  string `json:"group"`
	Reason string `json:"reason"`
}

type Error struct {
	Type     string  `json:"type"`
	MsgID    string  `json:"msg_id"`
	Code     string  `json:"code"`
	Message  string  `json:"message"`
	RefMsgID *string `json:"ref_msg_id"`
}

// Message type names.
const (
	TypeChallenge     = "challenge"
	TypeHello         = "hello"
	TypeWelcome       = "welcome"
	TypeHeartbeat     = "heartbeat"
	TypeHeartbeatAck  = "heartbeat_ack"
	TypeReady         = "ready"
	TypeOffer         = "offer"
	TypeNoWork        = "no_work"
	TypeAccept        = "accept"
	TypeDecline       = "decline"
	TypeStarted       = "started"
	TypeProgress      = "progress"
	TypeUploadRequest = "upload_request"
	TypeUploadURLs    = "upload_urls"
	TypePullAuthReq   = "pull_auth_request"
	TypePullAuth      = "pull_auth"
	TypeCommit        = "commit"
	TypeCommitAck     = "commit_ack"
	TypeAbort         = "abort"
	TypeRenewCert     = "renew_cert"
	TypeCertRenewed   = "cert_renewed"
	TypeCancel        = "cancel"
	TypePreempt       = "preempt"
	TypeRevoke        = "revoke"
	TypeTunnelOpen    = "tunnel_open"
	TypeError         = "error"
)

// NewMessage returns a pointer to a zero value of the struct for a message type.
func NewMessage(msgType string) (any, bool) {
	switch msgType {
	case TypeChallenge:
		return &Challenge{}, true
	case TypeHello:
		return &Hello{}, true
	case TypeWelcome:
		return &Welcome{}, true
	case TypeHeartbeat:
		return &Heartbeat{}, true
	case TypeHeartbeatAck:
		return &HeartbeatAck{}, true
	case TypeReady:
		return &Ready{}, true
	case TypeOffer:
		return &Offer{}, true
	case TypeNoWork:
		return &NoWork{}, true
	case TypeAccept:
		return &Accept{}, true
	case TypeDecline:
		return &Decline{}, true
	case TypeStarted:
		return &Started{}, true
	case TypeProgress:
		return &Progress{}, true
	case TypeUploadRequest:
		return &UploadRequest{}, true
	case TypeUploadURLs:
		return &UploadURLs{}, true
	case TypePullAuthReq:
		return &PullAuthRequest{}, true
	case TypePullAuth:
		return &PullAuth{}, true
	case TypeCommit:
		return &Commit{}, true
	case TypeCommitAck:
		return &CommitAck{}, true
	case TypeAbort:
		return &Abort{}, true
	case TypeRenewCert:
		return &RenewCert{}, true
	case TypeCertRenewed:
		return &CertRenewed{}, true
	case TypeCancel:
		return &Cancel{}, true
	case TypePreempt:
		return &Preempt{}, true
	case TypeRevoke:
		return &Revoke{}, true
	case TypeTunnelOpen:
		return &TunnelOpen{}, true
	case TypeError:
		return &Error{}, true
	}
	return nil, false
}

// Decode parses one frame into its typed struct.
func Decode(raw []byte) (any, error) {
	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, fmt.Errorf("bad frame: %w", err)
	}
	msg, ok := NewMessage(env.Type)
	if !ok {
		return nil, fmt.Errorf("unknown message type %q", env.Type)
	}
	if err := json.Unmarshal(raw, msg); err != nil {
		return nil, fmt.Errorf("bad %s: %w", env.Type, err)
	}
	return msg, nil
}
