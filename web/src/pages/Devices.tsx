import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { CopyButton, Empty, ErrorBox, Loading, StateBadge } from "../components/ui";
import { fmtRelative, fmtTime } from "../lib/format";

export function DevicesPage() {
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => unwrap(client.GET("/api/v1/devices")),
    refetchInterval: 15000,
  });
  return (
    <>
      <h1>My devices</h1>
      <EnrollDevice />
      <section className="card">
        <h2>Enrolled devices</h2>
        {devices.isLoading && <Loading />}
        <ErrorBox error={devices.error} />
        {devices.data && devices.data.length === 0 && <Empty>No devices yet. Create an enrollment code above.</Empty>}
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
  const create = useMutation({ mutationFn: () => unwrap(client.POST("/api/v1/devices/enrollment-codes")) });
  const cmd = create.data
    ? `pool-agent enroll --server ${window.location.origin} --code ${create.data.code} --name my-device`
    : "";
  return (
    <section className="card">
      <h2>Enroll a device</h2>
      <p className="muted">
        Install the agent on the computer, then run the command below on it. The code works once and expires after 10
        minutes. The device's private key is created on the device and never leaves it.
      </p>
      <ErrorBox error={create.error} />
      {create.data ? (
        <div className="notice" role="status">
          <p>
            Code <strong className="code-big">{create.data.code}</strong> — valid until {fmtTime(create.data.expires_at)}
          </p>
          <div className="row">
            <code className="break">{cmd}</code>
            <CopyButton text={cmd} label="Copy command" />
          </div>
          <p className="muted">
            New devices share nothing until you share them with a group here and allow that group in the agent's local
            policy file.
          </p>
        </div>
      ) : null}
      <button type="button" onClick={() => create.mutate()} disabled={create.isPending}>
        {create.data ? "Create another code" : "Create enrollment code"}
      </button>
    </section>
  );
}
