## What and why

<!-- What does this change do, and why is it needed? Link the issue or plan section. -->

## How I verified it

<!-- Commands you ran and their result. Say what you did NOT test. -->

- [ ] Unit tests added or updated
- [ ] `uv run pytest -q` passes
- [ ] `cd agent && go test ./...` passes (if the agent changed)
- [ ] Protocol schema regenerated and contract tests pass (if `protocol/` changed)

## Security

- [ ] This PR touches a security-sensitive path (`control-plane/pool_cp/{auth,authz,audit,ca,secrets}`, `agent/{sandbox,policy,enroll}`, `protocol/`) or login, crypto, input parsing, access control or data deletion. **If ticked, a security reviewer must approve, and I filled in the relevant part of `security/review-checklist.md` below.**
- [ ] No secrets, keys, tokens or real personal data in code, tests, fixtures or logs.
- [ ] New dependencies are listed below with their license.

<!-- Paste the relevant checklist items here, if the first box is ticked. -->

## Dependencies added

| Package | License |
|---|---|
| | |

## Notes for reviewers

<!-- Anything risky, uncertain, or that needs a decision. -->
