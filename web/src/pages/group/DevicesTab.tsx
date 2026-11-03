import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ConfirmDialog, Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";

const MANAGERS = new Set(["coordinator", "admin"]);
type Pick = { id: string; name: string; owner: string; action: "remove" | "revoke" };

export function DevicesTab({ group }: { group: Group }) {
  const { me } = useAuth();
  const qc = useQueryClient();
  const manager = MANAGERS.has(group.my_role ?? "");
  const [pick, setPick] = useState<Pick | null>(null);
  const path = { name: group.name };
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["group-devices", group.name] });
    void qc.invalidateQueries({ queryKey: ["removed-devices", group.name] });
  };
  const removed = useQuery({
    queryKey: ["removed-devices", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/removed-devices", { params: { path } })),
    enabled: manager,
  });
  const act = useMutation({
    mutationFn: async (p: Pick): Promise<void> => {
      if (p.action === "remove") {
        await unwrap(client.DELETE("/api/v1/groups/{name}/devices/{device_id}", { params: { path: { ...path, device_id: p.id } } }));
      } else {
        await unwrap(client.POST("/api/v1/devices/{device_id}/revoke", { params: { path: { device_id: p.id } } }));
      }
    },
    onSuccess: () => {
      setPick(null);
      refresh();
    },
  });
  const allow = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.POST("/api/v1/groups/{name}/devices/{device_id}/allow", { params: { path: { ...path, device_id: id } } })),
    onSuccess: refresh,
  });
  const devices = useQuery({
    queryKey: ["group-devices", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/devices", { params: { path: { name: group.name } } })),
    refetchInterval: 15000,
  });
  return (
    <section className="card">
      <h2>Computers shared with this group</h2>
      <p className="muted">
        Share your own computers from the "My computers" page.
        {manager && " As a coordinator or admin you can remove a computer from the group; it stays out until you let it back in."}
      </p>
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
                {(manager || me?.server_admin) && <th />}
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
                  {(manager || me?.server_admin) && (
                    <td className="row">
                      {manager && (
                        <button type="button" className="secondary small"
                                onClick={() => setPick({ id: d.id, name: d.name, owner: d.owner, action: "remove" })}>
                          Remove
                        </button>
                      )}
                      {me?.server_admin && (
                        <button type="button" className="danger small"
                                onClick={() => setPick({ id: d.id, name: d.name, owner: d.owner, action: "revoke" })}>
                          Revoke…
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {manager && removed.data && removed.data.length > 0 && (
        <>
          <h3>Removed from this group</h3>
          <ErrorBox error={allow.error} />
          <ul className="plain">
            {removed.data.map((d) => (
              <li key={d.id} className="row between">
                <span>
                  {d.name} <span className="muted">({d.owner}) · removed {fmtTime(d.removed_at)}</span>
                </span>
                <button type="button" className="secondary small" disabled={allow.isPending} onClick={() => allow.mutate(d.id)}>
                  Let back in
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      <ConfirmDialog
        open={pick !== null}
        title={pick?.action === "revoke" ? `Revoke ${pick?.name}?` : `Remove ${pick?.name ?? ""} from ${group.display_name}?`}
        confirmLabel={pick?.action === "revoke" ? "Revoke computer" : "Remove from group"}
        busy={act.isPending}
        error={act.error}
        onCancel={() => {
          setPick(null);
          act.reset();
        }}
        onConfirm={() => pick && act.mutate(pick)}
      >
        {pick?.action === "revoke" ? (
          <p>
            For a lost or stolen computer: it is cut off from the whole server at once, its work moves elsewhere, and its
            owner ({pick.owner}) must enroll it again.
          </p>
        ) : (
          <p>
            Its work for this group stops and moves to other computers. {pick?.owner} cannot add it back; you or another
            coordinator or admin can let it back in later.
          </p>
        )}
      </ConfirmDialog>
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
