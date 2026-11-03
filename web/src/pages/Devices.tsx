import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { CopyButton, Empty, ErrorBox, Loading, StateBadge } from "../components/ui";
import { RUN_COMMAND, enrollCommand, serverKind, shareCommand } from "../lib/agentCommands";
import { fmtRelative, fmtTime } from "../lib/format";

export function DevicesPage() {
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => unwrap(client.GET("/api/v1/devices")),
    refetchInterval: 15000,
  });
  return (
    <>
      <h1>My computers</h1>
      <EnrollDevice />
      <section className="card">
        <h2>Your computers</h2>
        {devices.isLoading && <Loading />}
        <ErrorBox error={devices.error} />
        {devices.data && devices.data.length === 0 && <Empty>No computers yet. Follow the steps above on the computer you want to share.</Empty>}
        {devices.data && devices.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>State</th>
                  <th>Online</th>
                  <th>OS</th>
                  <th>Shared with</th>
                  <th>Last seen</th>
                  <th>Certificate valid until</th>
                </tr>
              </thead>
              <tbody>
                {devices.data.map((d) => (
                  <tr key={d.id}>
                    <td>
                      <Link to={`/devices/${d.id}`}>{d.name}</Link>
                    </td>
                    <td>
                      <StateBadge state={d.state} />
                    </td>
                    <td>{d.online ? "yes" : "no"}</td>
                    <td>
                      {d.os}/{d.arch}
                    </td>
                    <td>{d.shared_with.join(", ") || <span className="muted">nobody</span>}</td>
                    <td>{fmtRelative(d.last_seen_at)}</td>
                    <td>{fmtTime(d.cert_not_after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function EnrollDevice() {
  const groups = useQuery({ queryKey: ["groups"], queryFn: () => unwrap(client.GET("/api/v1/groups")) });
  const create = useMutation({ mutationFn: () => unwrap(client.POST("/api/v1/devices/enrollment-codes")) });
  const origin = window.location.origin;
  const kind = serverKind(origin);
  const firstGroup = groups.data?.find((g) => g.state === "active")?.name ?? "GROUP";
  const enroll = create.data ? enrollCommand(origin, create.data.code) : "";
  const share = shareCommand(firstGroup);
  return (
    <section className="card">
      <h2>Share a computer</h2>
      <p className="muted">
        Run these steps on the computer you want to share. It needs Docker. It shares nothing until step 3, and you can
        stop everything at any time with <code>pool-agent kill</code>.
      </p>
      {kind === "insecure-remote" && (
        <p className="warn">
          This server uses plain http. The agent only connects to https servers (or to localhost for development), so
          ask the server admin to put HTTPS in front of it.
        </p>
      )}
      <ol className="steps">
        <li>
          <strong>Get the agent.</strong> Installers are coming; for now an administrator or developer builds the{" "}
          <code>pool-agent</code> program (see the project README).
        </li>
        <li>
          <strong>Connect it to your account.</strong>{" "}
          {create.data ? (
            <>
              Run this within 10 minutes (until {fmtTime(create.data.expires_at)}); the code works once:
              <span className="row">
                <code className="break">{enroll}</code>
                <CopyButton text={enroll} label="Copy" />
              </span>
            </>
          ) : (
            <>Create a one-time code, then run the command shown here.</>
          )}
          <ErrorBox error={create.error} />
          <button type="button" className={create.data ? "secondary small" : ""} onClick={() => create.mutate()} disabled={create.isPending}>
            {create.data ? "Create another code" : "Create code"}
          </button>
        </li>
        <li>
          <strong>Choose a group to share with.</strong> This also lists the computer in that group:
          <span className="row">
            <code className="break">{share}</code>
            <CopyButton text={share} label="Copy" />
          </span>
          <span className="muted small-text">
            Add <code>--cpu-percent 50 --memory 8Gi</code> to set limits, or <code>--only-when-idle 10m</code> to run work
            only when you are away.
          </span>
        </li>
        <li>
          <strong>Start it</strong> and leave it running: <code>{RUN_COMMAND}</code>
        </li>
      </ol>
      <p className="muted small-text">The computer's private key is created on the computer and never leaves it.</p>
    </section>
  );
}
