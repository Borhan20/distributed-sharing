import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { ConfirmDialog, Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";

const MANAGERS = new Set(["coordinator", "admin"]);

/**
 * Private images (docs/user-guide.md §4): the group keeps one read-only registry credential per registry
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
      <section className="card">
        <h2>Private images</h2>
        <p>
          Keep your group's images private in an organization on the GitHub container registry (ghcr.io) or Docker Hub.
          Each person pushes with their own account. The computers that run your jobs never get a password: for each
          task, this server gives them access to that one image for a few minutes, using a read-only token stored
          here.
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
            <p className="muted small-text">
              Use a token that can only <em>read</em>, ideally from an account made for this purpose with read access to the
              organization's images. GitHub: a personal access token <em>(classic)</em> with only{" "}
              <code>read:packages</code> (Settings → Developer settings → Personal access tokens → Tokens (classic));
              fine-grained tokens do not work. Docker Hub: a personal access token with the permission{" "}
              <em>Repo Read-only</em> (Account settings → Personal access tokens); the default,{" "}
              <em>Repo Public Read-only</em>, cannot read private images. The token is checked with the registry,
              stored encrypted, and never shown again.
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
