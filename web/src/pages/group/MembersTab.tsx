import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group, type Member } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { ConfirmDialog, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";

const ROLES = ["coordinator", "admin", "member", "contributor", "submitter"] as const;
type Role = (typeof ROLES)[number];

export function MembersTab({ group }: { group: Group }) {
  const { me } = useAuth();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const path = { name: group.name };
  const members = useQuery({
    queryKey: ["members", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/members", { params: { path } })),
  });
  const [removing, setRemoving] = useState<Member | null>(null);
  const isCoord = group.my_role === "coordinator";
  const changeRole = useMutation({
    mutationFn: ({ handle, role }: { handle: string; role: Role }) =>
      unwrap(client.PATCH("/api/v1/groups/{name}/members/{handle}", { params: { path: { ...path, handle } }, body: { role } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["members", group.name] }),
  });
  const remove = useMutation({
    mutationFn: (handle: string) =>
      unwrap(client.DELETE("/api/v1/groups/{name}/members/{handle}", { params: { path: { ...path, handle } } })),
    onSuccess: (_d, handle) => {
      setRemoving(null);
      if (handle === me?.handle) {
        void qc.invalidateQueries({ queryKey: ["groups"] });
        navigate("/");
      } else void qc.invalidateQueries({ queryKey: ["members", group.name] });
    },
  });
  const self = removing?.handle === me?.handle;
  return (
    <section className="card">
      <h2>Members</h2>
      {members.isLoading && <Loading />}
      <ErrorBox error={members.error} />
      <ErrorBox error={changeRole.error} />
      {members.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>Role</th>
                <th>Probation until</th>
                <th>Joined</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {members.data.map((m) => (
                <tr key={m.user_id}>
                  <td>
                    {m.handle}
                    {m.handle === me?.handle && <span className="muted"> (you)</span>}
                  </td>
                  <td>
                    {isCoord ? (
                      <select
                        aria-label={`Role of ${m.handle}`}
                        value={m.role}
                        disabled={changeRole.isPending}
                        onChange={(e) => changeRole.mutate({ handle: m.handle, role: e.target.value as Role })}
                      >
                        {ROLES.map((r) => (
                          <option key={r}>{r}</option>
                        ))}
                      </select>
                    ) : (
                      m.role
                    )}
                  </td>
                  <td>{m.probation_until ? fmtTime(m.probation_until) : "–"}</td>
                  <td>{fmtTime(m.joined_at)}</td>
                  <td>
                    {(isCoord || m.handle === me?.handle) && (
                      <button type="button" className="danger small" onClick={() => setRemoving(m)}>
                        {m.handle === me?.handle ? "Leave" : "Remove"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ConfirmDialog
        open={!!removing}
        title={self ? `Leave ${group.name}?` : `Remove ${removing?.handle} from ${group.name}?`}
        confirmLabel={self ? "Leave group" : "Remove member"}
        busy={remove.isPending}
        error={remove.error}
        onCancel={() => {
          setRemoving(null);
          remove.reset();
        }}
        onConfirm={() => removing && remove.mutate(removing.handle)}
      >
        <p>This takes effect at once:</p>
        <ul>
          <li>{self ? "Your" : "Their"} running and queued jobs in this group are cancelled.</li>
          <li>{self ? "Your" : "Their"} devices stop serving this group.</li>
          <li>An audit event is written.</li>
        </ul>
      </ConfirmDialog>
    </section>
  );
}
