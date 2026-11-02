# ADR 0003: Agents pull work over outbound connections only

| | |
|---|---|
| **Status** | Accepted (proposed for team review) |
| **Date** | 2026-11-02 |
| **Deciders** | Yaana R&D (to confirm at team review) |
| **Plan link** | [`plan.md` §3 D3](../../plan.md#3-key-decisions), §7.1, §7.12, §8.7 |

## Context

Member devices sit behind home and office routers, NAT and firewalls. Many
owners cannot open inbound ports, and they should not have to. The device
owner must stay in control of what runs.

## Decision

We will have each agent open one outbound WebSocket to the control plane.
The agent sends `ready` with its free capacity. The server answers with an
`offer` or `no_work`. The agent then sends `accept` or `decline`. The server
never connects to a device. Devices have no open inbound ports in Phases 1
and 2.

## Consequences

- Good: works behind NAT and corporate firewalls with no network changes.
- Good: the agent decides when to ask for work, so local policy (time
  windows, idle state, kill switch) applies before any offer arrives.
- Good: a declined task goes straight back to the queue.
- Bad: the server can only push stop signals (`cancel`, `revoke`) while the
  agent is connected. Mitigation: heartbeats return a `stale` list, so every
  agent learns within one heartbeat which attempts it must stop.
- Bad: services and gang jobs need devices to reach each other. That needs
  the Phase 3 overlay network (Headscale/WireGuard), not inbound ports.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Server pushes work to an agent API | Needs inbound ports or a relay on every device. Breaks behind NAT. |
| Polling over plain HTTPS | Workable, but slower to react and more requests. A WebSocket gives one long-lived authenticated session. |

## Security review

Security-sensitive: the WebSocket protocol is under `protocol/` (plan §8.10).
