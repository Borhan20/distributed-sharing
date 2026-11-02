# Security policy

pool runs code from group members on other people's computers. We take
reports about sandbox escapes, cross-group leaks and authentication or
authorization bugs very seriously.

## Supported versions

The project is pre-release. There are no supported releases yet.

| Version | Supported |
|---|---|
| `main` branch | Best effort, until the first release |
| Any release | None published yet |

When releases start, this table will list which versions get security fixes.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Send a private report to: `[INSERT SECURITY CONTACT]`
(placeholder: the team will add a dedicated address or GitHub private
vulnerability reporting before the public release).

Please include:

- What is affected (component, version or commit).
- Steps to reproduce, or a proof of concept.
- What an attacker could do with it.
- Whether you want to be credited, and how.

Use synthetic data in your report. Do not include real personal data,
credentials or other people's job data.

## What happens next

These are targets, not promises, until the team confirms them:

1. We acknowledge your report within 3 working days.
2. We confirm or reject the issue and agree a severity within 10 working days.
3. We fix critical and high issues before any other work, and agree a
   disclosure date with you (usually within 90 days of the report).
4. We publish an advisory with the fix and credit you if you want.

## Scope

In scope: the control plane, scheduler, Go agent, sandbox configuration,
agent protocol, CLI, web UI and the deployment files in this repository.

Especially interesting:

- A job that reads host files, reaches the LAN or cloud metadata, or
  escapes its container.
- A job or member of one group that reads another group's data.
- A late or forged result committed for a task (fencing bypass).
- Authentication or authorization bypass, including on the agent protocol.
- Anything that makes an agent run work its owner's local policy forbids.

Out of scope: vulnerabilities in third-party components (Docker, gVisor,
Kata, Keycloak, PostgreSQL and so on) unless our configuration makes them
worse. Please report those to the upstream project.

## Known limits

Some risks cannot be fixed in software on consumer hardware. They are
listed in `security/threat-model.md` §5, for example that a device owner
with admin rights can read the memory of a job running on their machine.
