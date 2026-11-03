import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { ConfirmDialog, Empty, ErrorBox, Loading } from "../components/ui";
import { TokenSteps } from "../components/TokenSteps";
import { fmtTime } from "../lib/format";

/**
 * My own registry tokens (docs/user-guide.md §6, Private images): used only for jobs I submit, in any of my
 * groups. Computers never get them, only short-lived access to the one image a task needs.
 */
export function MyImagesPage() {
  const qc = useQueryClient();
  const [registry, setRegistry] = useState("docker.io");
  const [other, setOther] = useState("");
  const [username, setUsername] = useState("");
  const [token, setToken] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["my-registries"],
    queryFn: () => unwrap(client.GET("/api/v1/me/registries")),
  });
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PUT("/api/v1/me/registries", {
          body: { registry: registry === "other" ? other : registry, username, token },
        }),
      ),
    onSuccess: () => {
      setToken("");
      void qc.invalidateQueries({ queryKey: ["my-registries"] });
    },
  });
  const remove = useMutation({
    mutationFn: (r: string) => unwrap(client.DELETE("/api/v1/me/registries/{registry}", { params: { path: { registry: r } } })),
    onSuccess: () => {
      setRemoving(null);
      void qc.invalidateQueries({ queryKey: ["my-registries"] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  return (
    <>
      <header className="page-head">
        <h1>My images</h1>
        <p className="muted">
          Public images need nothing here. If your image is <strong>private</strong>, add a read-only token for its
          registry once. It is used only for jobs you submit, in any of your groups. The computers that run them never
          see it: for each task, the server gives them access to that one image for a few minutes.
        </p>
      </header>
      <section className="card">
        <h2>My tokens</h2>
        {list.isLoading && <Loading />}
        <ErrorBox error={list.error} />
        {list.data && list.data.length === 0 && <Empty>No tokens yet.</Empty>}
        {list.data && list.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Registry</th>
                  <th>Account</th>
                  <th>Updated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.data.map((r) => (
                  <tr key={r.registry}>
                    <td>
                      <code>{r.registry}</code>
                    </td>
                    <td>{r.username}</td>
                    <td>{fmtTime(r.updated_at)}</td>
                    <td>
                      <button type="button" className="secondary small" onClick={() => setRemoving(r.registry)}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card">
        <h2>Add or replace a token</h2>
        <form onSubmit={submit}>
          <label>
            Where is your image?
            <select value={registry} onChange={(e) => setRegistry(e.target.value)}>
              <option value="docker.io">Docker Hub (docker.io, e.g. yourname/app:1)</option>
              <option value="ghcr.io">GitHub container registry (ghcr.io/…)</option>
              <option value="other">Another registry…</option>
            </select>
          </label>
          {registry === "other" && (
            <label>
              Registry host
              <input value={other} onChange={(e) => setOther(e.target.value.trim())} placeholder="registry.example.org" />
            </label>
          )}
          <p>
            <strong>How to make a read-only token</strong>
          </p>
          <TokenSteps registry={registry} />
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
            The server checks the token with the registry, stores it encrypted, and never shows it again. When it
            expires, add a new one here.
          </p>
          <ErrorBox error={save.error} />
          {save.isSuccess && <p className="ok">Saved: {save.data.registry} as {save.data.username}.</p>}
          <button type="submit" disabled={save.isPending || !username || !token || (registry === "other" && !other)}>
            Save token
          </button>
        </form>
      </section>
      <ConfirmDialog
        open={removing !== null}
        title={`Remove your token for ${removing ?? ""}?`}
        confirmLabel="Remove token"
        busy={remove.isPending}
        error={remove.error}
        onCancel={() => {
          setRemoving(null);
          remove.reset();
        }}
        onConfirm={() => removing && remove.mutate(removing)}
      >
        <p>
          Your jobs with private images from this registry then use the group's token if it has one, and otherwise
          cannot pull them.
        </p>
      </ConfirmDialog>
    </>
  );
}
