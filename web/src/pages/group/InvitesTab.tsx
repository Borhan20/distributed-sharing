import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group, type Invite } from "../../api/client";
import { CopyButton, Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";

type InviteRole = "admin" | "member" | "contributor" | "submitter";

export function InvitesTab({ group }: { group: Group }) {
  const qc = useQueryClient();
  const path = { name: group.name };
  const [role, setRole] = useState<InviteRole>("member");
  const [maxUses, setMaxUses] = useState(1);
  const [hours, setHours] = useState(72);
  const [created, setCreated] = useState<Invite | null>(null);
  const invites = useQuery({
    queryKey: ["invites", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/invites", { params: { path } })),
  });
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/groups/{name}/invites", {
          params: { path },
          body: { role, max_uses: maxUses, expires_in_hours: hours },
        }),
      ),
    onSuccess: (inv) => {
      setCreated(inv);
      void qc.invalidateQueries({ queryKey: ["invites", group.name] });
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.DELETE("/api/v1/groups/{name}/invites/{invite_id}", { params: { path: { ...path, invite_id: id } } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["invites", group.name] }),
  });
  // The token goes in the fragment (#), which browsers never send to a server.
  const link = created?.token ? `${window.location.origin}/invite#token=${encodeURIComponent(created.token)}` : "";
  const roles: InviteRole[] = group.my_role === "coordinator" ? ["admin", "member", "contributor", "submitter"] : ["member", "contributor", "submitter"];
  return (
    <>
      <section className="card">
        <h2>Create an invite link</h2>
        <form
          className="row wrap"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <label>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value as InviteRole)}>
              {roles.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
          <label>
            Max uses
            <input type="number" min={1} max={500} value={maxUses} onChange={(e) => setMaxUses(Number(e.target.value))} />
          </label>
          <label>
            Valid for (hours)
            <input type="number" min={1} max={720} value={hours} onChange={(e) => setHours(Number(e.target.value))} />
          </label>
          <button type="submit" disabled={create.isPending}>
            Create invite
          </button>
        </form>
        <ErrorBox error={create.error} />
        {created && link && (
          <div className="notice" role="status">
            <p>
              <strong>Copy this link now — it is shown only once.</strong> Anyone with it can join as{" "}
              <strong>{created.role}</strong> until {fmtTime(created.expires_at)} ({created.max_uses} use(s)).
            </p>
            <div className="row">
              <code className="break">{link}</code>
              <CopyButton text={link} />
            </div>
          </div>
        )}
      </section>
      <section className="card">
        <h2>Invites</h2>
        {invites.isLoading && <Loading />}
        <ErrorBox error={invites.error ?? revoke.error} />
        {invites.data && invites.data.length === 0 && <Empty>No invites.</Empty>}
        {invites.data && invites.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Uses</th>
                  <th>Expires</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {invites.data.map((i) => {
                  const expired = new Date(i.expires_at).getTime() < Date.now();
                  const status = i.revoked ? "revoked" : expired ? "expired" : i.uses >= i.max_uses ? "used up" : "active";
                  return (
                    <tr key={i.id}>
                      <td>{i.role}</td>
                      <td>
                        {i.uses} / {i.max_uses}
                      </td>
                      <td>{fmtTime(i.expires_at)}</td>
                      <td>{status}</td>
                      <td>
                        {status === "active" && (
                          <button type="button" className="danger small" onClick={() => revoke.mutate(i.id)}>
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
