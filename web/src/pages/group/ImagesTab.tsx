import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { ConfirmDialog, Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";
import { TokenSteps } from "../../components/TokenSteps";
import { Link } from "react-router-dom";

const MANAGERS = new Set(["coordinator", "admin"]);

/**
 * Private images (docs/user-guide.md §6): the group keeps one read-only registry credential per registry
 * on the server; computers only get short-lived access to the one image a task needs.
 */
export function ImagesTab({ group }: { group: Group }) {
  const qc = useQueryClient();
  const path = { name: group.name };
  const manager = MANAGERS.has(group.my_role ?? "");
  const [registry, setRegistry] = useState("ghcr.io");
  const [other, setOther] = useState("");
  const [username, setUsername] = useState("");
  const [token, setToken] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["registries", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/registries", { params: { path } })),
  });
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PUT("/api/v1/groups/{name}/registries", {
          params: { path },
          body: { registry: registry === "other" ? other : registry, username, token },
        }),
      ),
    onSuccess: () => {
      setToken("");
      void qc.invalidateQueries({ queryKey: ["registries", group.name] });
    },
  });
  const remove = useMutation({
    mutationFn: (r: string) =>
      unwrap(client.DELETE("/api/v1/groups/{name}/registries/{registry}", { params: { path: { ...path, registry: r } } })),
    onSuccess: () => {
      setRemoving(null);
      void qc.invalidateQueries({ queryKey: ["registries", group.name] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  return (
    <>
      <BuiltinRegistry group={group} />
      <section className="card">
        <h2>Private images</h2>
        <p>
          Keep your group's images private in an organization on the GitHub container registry (ghcr.io) or Docker Hub.
          Each person pushes with their own account. The computers that run your jobs never get a password: for each
          task, this server gives them access to that one image for a few minutes, using a read-only token stored
          here.
        </p>
        <p className="muted">
          An image in someone's personal account? Its owner adds their own token under{" "}
          <Link to="/images">My images</Link> instead; it is used only for their jobs.
        </p>
        {list.isLoading && <Loading />}
        <ErrorBox error={list.error} />
        {list.data && list.data.length === 0 && (
          <Empty>No registry set up: only public images can run in this group.</Empty>
        )}
        {list.data && list.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Registry</th>
                  <th>Read-only account</th>
                  <th>Set by</th>
                  <th>Updated</th>
                  {manager && <th />}
                </tr>
              </thead>
              <tbody>
                {list.data.map((r) => (
                  <tr key={r.registry}>
                    <td>
                      <code>{r.registry}</code>
                    </td>
                    <td>{r.username}</td>
                    <td>{r.updated_by ?? "–"}</td>
                    <td>{fmtTime(r.updated_at)}</td>
                    {manager && (
                      <td>
                        <button type="button" className="secondary small" onClick={() => setRemoving(r.registry)}>
                          Remove
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {manager && (
        <section className="card">
          <h2>Add or replace a registry token</h2>
          <form onSubmit={submit}>
            <label>
              Registry
              <select value={registry} onChange={(e) => setRegistry(e.target.value)}>
                <option value="ghcr.io">GitHub container registry (ghcr.io)</option>
                <option value="docker.io">Docker Hub (docker.io)</option>
                <option value="other">Another registry…</option>
              </select>
            </label>
            {registry === "other" && (
              <label>
                Registry host
                <input value={other} onChange={(e) => setOther(e.target.value.trim())} placeholder="registry.example.org" />
              </label>
            )}
            <label>
              Account name
              <input value={username} onChange={(e) => setUsername(e.target.value.trim())} autoComplete="off" required />
            </label>
            <label>
              Read-only token
              <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="new-password"
                     required />
            </label>
            <p>
              <strong>How to make a read-only token</strong> (ideally from an account made for this purpose, with read
              access to the organization's images):
            </p>
            <TokenSteps registry={registry === "other" ? "other" : registry} />
            <p className="muted small-text">
              The token is checked with the registry, stored encrypted, and never shown again.
            </p>
            <ErrorBox error={save.error} />
            <button type="submit" disabled={save.isPending || !username || !token}>
              Save token
            </button>
          </form>
        </section>
      )}
      <ConfirmDialog
        open={removing !== null}
        title={`Remove the token for ${removing ?? ""}?`}
        confirmLabel="Remove token"
        busy={remove.isPending}
        error={remove.error}
        onCancel={() => {
          setRemoving(null);
          remove.reset();
        }}
        onConfirm={() => removing && remove.mutate(removing)}
      >
        <p>Jobs with private images from this registry stop working in this group until a token is added again.</p>
      </ConfirmDialog>
    </>
  );
}

/**
 * The server's built-in registry (docs/design/pool-run.md §3.1): where `pool run` pushes your images, and how
 * long images nobody uses are kept. Coordinators can change that for the group.
 */
function BuiltinRegistry({ group }: { group: Group }) {
  const qc = useQueryClient();
  const info = useQuery({
    queryKey: ["group-registry", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/registry", { params: { path: { name: group.name } } })),
  });
  const [days, setDays] = useState<string>("");
  const save = useMutation({
    mutationFn: (value: number | null) =>
      unwrap(
        client.PATCH("/api/v1/groups/{name}", {
          params: { path: { name: group.name } },
          body: { registry_retention_days: value },
        }),
      ),
    onSuccess: () => {
      setDays("");
      void qc.invalidateQueries({ queryKey: ["group-registry", group.name] });
    },
  });
  if (!info.data?.enabled) return null;
  const r = info.data;
  const coordinator = group.my_role === "coordinator";
  return (
    <section className="card">
      <h2>The group's registry</h2>
      <p>
        <code>pool run</code> builds your Dockerfile on your computer and pushes the image here for you, under your own
        name: <code className="break">{r.repository_prefix}/…</code>. Only you can push or use your images there, and
        nobody needs a Docker Hub or GitHub account.
      </p>
      <p className="muted">
        Images nobody has used for <strong>{r.retention_days} days</strong> are deleted
        {r.group_retention_days ? " (this group's setting)" : ` (the server's default)`}. Using an image in a job keeps
        it.
      </p>
      {coordinator && (
        <form
          className="row wrap"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(days.trim() ? Number(days) : null);
          }}
        >
          <label>
            Keep unused images for (days)
            <input
              type="number"
              min={1}
              max={3650}
              value={days}
              placeholder={String(r.retention_days)}
              onChange={(e) => setDays(e.target.value)}
            />
          </label>
          <button type="submit" className="secondary" disabled={save.isPending || !days.trim()}>
            Save
          </button>
          {r.group_retention_days !== null && r.group_retention_days !== undefined && (
            <button type="button" className="secondary" disabled={save.isPending} onClick={() => save.mutate(null)}>
              Use the server's default ({r.default_retention_days} days)
            </button>
          )}
        </form>
      )}
      <ErrorBox error={info.error ?? save.error} />
    </section>
  );
}
