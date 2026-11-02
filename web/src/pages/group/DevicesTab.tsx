import { useQuery } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { Empty, ErrorBox, Loading } from "../../components/ui";

export function DevicesTab({ group }: { group: Group }) {
  const devices = useQuery({
    queryKey: ["group-devices", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/devices", { params: { path: { name: group.name } } })),
    refetchInterval: 15000,
  });
  return (
    <section className="card">
      <h2>Devices shared with this group</h2>
      <p className="muted">Share your own devices from the "My devices" page.</p>
      {devices.isLoading && <Loading />}
      <ErrorBox error={devices.error} />
      {devices.data && devices.data.length === 0 && <Empty>No devices are shared with this group yet.</Empty>}
      {devices.data && devices.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Device</th>
                <th>Owner</th>
                <th>OS</th>
                <th>Online</th>
                <th>Accepting work now</th>
                <th>Tiers</th>
                <th>CPU / RAM / GPUs</th>
                <th>Always on</th>
              </tr>
            </thead>
            <tbody>
              {devices.data.map((d) => (
                <tr key={d.id}>
                  <td>{d.name}</td>
                  <td>{d.owner}</td>
                  <td>{d.os}</td>
                  <td>{d.online ? "yes" : "no"}</td>
                  <td>{d.active_now ? "yes" : "no"}</td>
                  <td>{d.tiers.join(", ") || "–"}</td>
                  <td>
                    {d.cpu_cores ?? "?"} cores / {d.memory_mb ? `${Math.round(d.memory_mb / 1024)} GiB` : "?"} / {d.gpus}
                  </td>
                  <td>{d.always_on ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

type UsageRow = { member: string; cpu_hours: number; gpu_hours: number; attempts: number };

export function UsageTab({ group }: { group: Group }) {
  const usage = useQuery({
    queryKey: ["usage", group.name],
    queryFn: async () =>
      (await unwrap(client.GET("/api/v1/groups/{name}/usage", { params: { path: { name: group.name }, query: { days: 7 } } }))) as unknown as UsageRow[],
  });
  return (
    <section className="card">
      <h2>Usage, last 7 days</h2>
      <p className="muted">Everyone in the group can see this. Fair share runs members with less usage first.</p>
      {usage.isLoading && <Loading />}
      <ErrorBox error={usage.error} />
      {usage.data && usage.data.length === 0 && <Empty>No usage recorded yet.</Empty>}
      {usage.data && usage.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>CPU-hours</th>
                <th>GPU-hours</th>
                <th>Attempts</th>
              </tr>
            </thead>
            <tbody>
              {usage.data.map((u) => (
                <tr key={u.member}>
                  <td>{u.member}</td>
                  <td>{u.cpu_hours.toFixed(2)}</td>
                  <td>{u.gpu_hours.toFixed(2)}</td>
                  <td>{u.attempts}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
