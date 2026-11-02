import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { AuditTable } from "../components/AuditTable";
import { ConfirmDialog, Empty, ErrorBox, Loading, StateBadge } from "../components/ui";
import { fmtRelative, fmtTime } from "../lib/format";

const SHARERS = new Set(["coordinator", "admin", "member", "contributor"]);

export function DevicePage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const path = { device_id: id };
  const [revoking, setRevoking] = useState(false);
  const [shareGroup, setShareGroup] = useState("");
  const device = useQuery({
    queryKey: ["device", id],
    queryFn: () => unwrap(client.GET("/api/v1/devices/{device_id}", { params: { path } })),
    refetchInterval: 15000,
  });
  const groups = useQuery({ queryKey: ["groups"], queryFn: () => unwrap(client.GET("/api/v1/groups")) });
  const history = useQuery({
    queryKey: ["device-history", id],
    queryFn: () => unwrap(client.GET("/api/v1/devices/{device_id}/history", { params: { path, query: { limit: 200 } } })),
    refetchInterval: 15000,
  });
  const audit = useQuery({
    queryKey: ["device-audit", id],
    queryFn: () => unwrap(client.GET("/api/v1/devices/{device_id}/audit", { params: { path, query: { limit: 500 } } })),
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["device", id] });
    void qc.invalidateQueries({ queryKey: ["devices"] });
    void qc.invalidateQueries({ queryKey: ["device-audit", id] });
  };
  const share = useMutation({
    mutationFn: (group: string) =>
      unwrap(client.PUT("/api/v1/devices/{device_id}/shares/{group_name}", { params: { path: { ...path, group_name: group } } })),
    onSuccess: refresh,
  });
  const unshare = useMutation({
    mutationFn: (group: string) =>
      unwrap(client.DELETE("/api/v1/devices/{device_id}/shares/{group_name}", { params: { path: { ...path, group_name: group } } })),
    onSuccess: refresh,
  });
  const patch = useMutation({
    mutationFn: (always_on: boolean) => unwrap(client.PATCH("/api/v1/devices/{device_id}", { params: { path }, body: { always_on } })),
    onSuccess: refresh,
  });
  const revoke = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/devices/{device_id}/revoke", { params: { path } })),
    onSuccess: () => {
      setRevoking(false);
      refresh();
    },
  });

  if (device.isLoading) return <Loading />;
  if (!device.data) return <ErrorBox error={device.error ?? new Error("device not found")} />;
  const d = device.data;
  const shareable = (groups.data ?? []).filter(
    (g) => g.state === "active" && SHARERS.has(g.my_role ?? "") && !d.shared_with.includes(g.name),
  );
  const caps = d.capabilities as { cpu_cores?: number; memory_mb?: number; tiers?: string[]; gpus?: unknown[] };
  const reported = d.reported_shares as { group: string; active_now?: boolean }[];
  const active = d.state === "active";
  return (
    <>
      <header className="page-head">
        <p className="muted">
          <Link to="/devices">My devices</Link> / device
        </p>
        <h1>
          {d.name} <StateBadge state={d.state} />
        </h1>
        <p className="muted">
          {d.online ? "Online" : "Offline"} · last seen {fmtRelative(d.last_seen_at)} · agent {d.agent_version || "?"} ·
          certificate valid until {fmtTime(d.cert_not_after)}
        </p>
      </header>
      <div className="grid2">
        <section className="card">
          <h2>Capabilities</h2>
          <dl className="kv">
            <dt>OS</dt>
            <dd>
              {d.os}/{d.arch}
            </dd>
            <dt>CPU / RAM</dt>
            <dd>
              {caps.cpu_cores ?? "?"} cores / {caps.memory_mb ? `${Math.round(caps.memory_mb / 1024)} GiB` : "?"}
            </dd>
            <dt>GPUs</dt>
            <dd>{caps.gpus?.length ?? 0}</dd>
            <dt>Isolation tiers</dt>
            <dd>{caps.tiers?.join(", ") || "–"}</dd>
            <dt>Owner active now</dt>
            <dd>{d.owner_active ? "yes" : "no"}</dd>
            <dt>Reliability</dt>
            <dd>{d.reliability}</dd>
            <dt>Org-owned, dedicated</dt>
            <dd>{d.org_owned_dedicated ? "yes (set by a server admin)" : "no"}</dd>
            <dt>Policy digest</dt>
            <dd>
              <code className="small-text break">{d.policy_digest || "–"}</code>
            </dd>
          </dl>
          <label className="check">
            <input
              type="checkbox"
              checked={d.always_on}
              disabled={!active || patch.isPending}
              onChange={(e) => patch.mutate(e.target.checked)}
            />{" "}
            Always on (may host long-running services later)
          </label>
          <ErrorBox error={patch.error} />
        </section>
        <section className="card">
          <h2>Sharing</h2>
          <p className="muted">
            A group gets work from this device only when you share it here <em>and</em> the agent's local policy file
            allows that group. The agent always has the final say.
          </p>
          {d.shared_with.length === 0 && <Empty>Not shared with any group.</Empty>}
          <ul className="plain">
            {d.shared_with.map((g) => {
              const r = reported.find((x) => x.group === g);
              return (
                <li key={g} className="row between">
                  <span>
                    <Link to={`/groups/${g}`}>{g}</Link>{" "}
                    <span className="muted">
                      {r ? (r.active_now ? "· local policy: accepting now" : "· local policy: not now") : "· not in local policy"}
                    </span>
                  </span>
                  <button type="button" className="secondary small" disabled={unshare.isPending} onClick={() => unshare.mutate(g)}>
                    Stop sharing
                  </button>
                </li>
              );
            })}
          </ul>
          {active && shareable.length > 0 && (
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                if (shareGroup) share.mutate(shareGroup);
              }}
            >
              <select aria-label="Group to share with" value={shareGroup} onChange={(e) => setShareGroup(e.target.value)}>
                <option value="">Choose a group…</option>
                {shareable.map((g) => (
                  <option key={g.id} value={g.name}>
                    {g.display_name} ({g.name})
                  </option>
                ))}
              </select>
              <button type="submit" disabled={!shareGroup || share.isPending}>
                Share
              </button>
            </form>
          )}
          <ErrorBox error={share.error ?? unshare.error} />
        </section>
      </div>
      <section className="card">
        <h2>What ran here</h2>
        {history.isLoading && <Loading />}
        <ErrorBox error={history.error} />
        {history.data && history.data.length === 0 && <Empty>Nothing has run on this device.</Empty>}
        {history.data && history.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Group</th>
                  <th>Submitted by</th>
                  <th>Job / task</th>
                  <th>Image</th>
                  <th>Command</th>
                  <th>Tier</th>
                  <th>State</th>
                  <th>Exit</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((h) => (
                  <tr key={h.attempt_id}>
                    <td>{fmtTime(h.started_at ?? h.created_at)}</td>
                    <td>{h.group}</td>
                    <td>{h.submitter}</td>
                    <td>
                      <Link to={`/jobs/${h.job_id}`}>{h.job_name}</Link> #{h.task_index}
                    </td>
                    <td>
                      <code className="small-text break">{h.image}</code>
                    </td>
                    <td>
                      <code className="small-text break">{h.command.join(" ")}</code>
                    </td>
                    <td>{h.tier ?? "–"}</td>
                    <td>
                      <StateBadge state={h.state} />
                      {h.end_reason ? <span className="muted small-text"> {h.end_reason}</span> : null}
                    </td>
                    <td>{h.exit_code ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card">
        <h2>Device audit log</h2>
        <ErrorBox error={audit.error} />
        {audit.data && audit.data.length === 0 && <Empty>No events.</Empty>}
        {audit.data && audit.data.length > 0 && <AuditTable rows={audit.data} />}
      </section>
      {active && (
        <section className="card danger-zone">
          <h2>Revoke this device</h2>
          <p>
            Use this if the device is lost or stolen, or you stop using it. Its certificate goes on the deny list at
            once, its running work moves to other devices, and it must be enrolled again to come back.
          </p>
          <button type="button" className="danger" onClick={() => setRevoking(true)}>
            Revoke {d.name}…
          </button>
        </section>
      )}
      <ConfirmDialog
        open={revoking}
        title={`Revoke device "${d.name}"?`}
        confirmLabel={`Revoke ${d.name}`}
        busy={revoke.isPending}
        error={revoke.error}
        onCancel={() => {
          setRevoking(false);
          revoke.reset();
        }}
        onConfirm={() => revoke.mutate()}
      >
        <p>
          The device <strong>{d.name}</strong> ({d.os}/{d.arch}) will be disconnected and can no longer get work. This
          cannot be undone; you would need to enroll it again.
        </p>
      </ConfirmDialog>
    </>
  );
}
