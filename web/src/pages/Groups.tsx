import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Empty, ErrorBox, Loading, StateBadge } from "../components/ui";
import { fmtTime } from "../lib/format";

const TEMPLATES = [
  { id: "research-team", label: "Research team — coordinator approves, permanent" },
  { id: "dev-team", label: "Dev team — coordinator approves, permanent" },
  { id: "student-group", label: "Student group — 2 members approve, 7-day probation" },
  { id: "course", label: "Course — coordinator approves, closes after ~120 days" },
  { id: "hackathon", label: "Hackathon — 1 member approves, closes after 2 days" },
] as const;
type Template = (typeof TEMPLATES)[number]["id"];

export function GroupsPage() {
  const { me } = useAuth();
  const groups = useQuery({ queryKey: ["groups"], queryFn: () => unwrap(client.GET("/api/v1/groups")) });
  return (
    <>
      <h1>Groups</h1>
      <section className="card">
        <h2>Your groups</h2>
        {groups.isLoading && <Loading />}
        <ErrorBox error={groups.error} />
        {groups.data && groups.data.length === 0 && <Empty>You are not in any group yet.</Empty>}
        {groups.data && groups.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Your role</th>
                  <th>Approval rule</th>
                  <th>State</th>
                  <th>Expires</th>
                </tr>
              </thead>
              <tbody>
                {groups.data.map((g: Group) => (
                  <tr key={g.id}>
                    <td>
                      <Link to={`/groups/${g.name}`}>{g.display_name}</Link> <span className="muted">({g.name})</span>
                    </td>
                    <td>{g.my_role}</td>
                    <td>
                      {g.approval_rule}
                      {g.approval_rule === "any-k-members" ? ` (k=${g.approval_k})` : ""}
                    </td>
                    <td>
                      <StateBadge state={g.state} />
                    </td>
                    <td>{g.expires_at ? fmtTime(g.expires_at) : "never"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <div className="grid2">
        {me?.can_create_groups === false ? (
          <section className="card">
            <h2>Create a group</h2>
            <p className="muted">
              On this server, groups are created by a server admin. Ask one to create your group and invite you as its
              coordinator.
            </p>
          </section>
        ) : (
          <CreateGroup />
        )}
        <RequestToJoin />
      </div>
    </>
  );
}

function CreateGroup() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [template, setTemplate] = useState<Template>("research-team");
  const create = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/groups", { body: { name, display_name: displayName || name, template } })),
    onSuccess: (g) => {
      void qc.invalidateQueries({ queryKey: ["groups"] });
      navigate(`/groups/${g.name}`);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <section className="card">
      <h2>Create a group</h2>
      <form onSubmit={submit}>
        <label>
          Name (used in URLs and job files)
          <input
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase())}
            pattern="[a-z0-9][a-z0-9\-]{1,62}"
            title="2-63 characters: lower-case letters, digits and dashes"
            placeholder="ml-lab"
            required
          />
        </label>
        <label>
          Display name
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="ML lab" />
        </label>
        <label>
          Template
          <select value={template} onChange={(e) => setTemplate(e.target.value as Template)}>
            {TEMPLATES.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
        <ErrorBox error={create.error} />
        <button type="submit" disabled={create.isPending}>
          Create group
        </button>
      </form>
    </section>
  );
}

function RequestToJoin() {
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const join = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/groups/{name}/join-requests", { params: { path: { name } }, body: { message } })),
  });
  return (
    <section className="card">
      <h2>Ask to join a group</h2>
      <p className="muted">Have an invite link? Just open it. Otherwise ask the group by name.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          join.mutate();
        }}
      >
        <label>
          Group name
          <input value={name} onChange={(e) => setName(e.target.value.toLowerCase())} required />
        </label>
        <label>
          Message (optional)
          <textarea value={message} onChange={(e) => setMessage(e.target.value)} maxLength={500} rows={2} />
        </label>
        <ErrorBox error={join.error} />
        {join.isSuccess && <p className="ok">Request sent. Approvals needed: {join.data.needed}.</p>}
        <button type="submit" disabled={join.isPending}>
          Send request
        </button>
      </form>
    </section>
  );
}
