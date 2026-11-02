import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group, type Job } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Empty, ErrorBox, Loading, StateBadge } from "../../components/ui";
import { EXAMPLE_JOB, JobParseError, parseJobText, taskCount } from "../../lib/jobspec";
import { fmtTime } from "../../lib/format";

const SUBMITTERS = new Set(["coordinator", "admin", "member", "submitter"]);
const MANAGERS = new Set(["coordinator", "admin"]);

export function JobsTab({ group }: { group: Group }) {
  const { me } = useAuth();
  const qc = useQueryClient();
  const jobs = useQuery({
    queryKey: ["jobs", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/jobs", { params: { path: { name: group.name } } })),
    refetchInterval: 5000,
  });
  const cancel = useMutation({
    mutationFn: (id: string) => unwrap(client.POST("/api/v1/jobs/{job_id}/cancel", { params: { path: { job_id: id } } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["jobs", group.name] }),
  });
  const canSubmit = SUBMITTERS.has(group.my_role ?? "") && group.state === "active";
  const canCancel = (j: Job) =>
    (j.submitter === me?.handle || MANAGERS.has(group.my_role ?? "")) && (j.state === "pending" || j.state === "running");

  return (
    <>
      {canSubmit && <SubmitJob group={group} />}
      <section className="card">
        <h2>Jobs</h2>
        {jobs.isLoading && <Loading />}
        <ErrorBox error={jobs.error} />
        <ErrorBox error={cancel.error} />
        {jobs.data && jobs.data.length === 0 && <Empty>No jobs yet.</Empty>}
        {jobs.data && jobs.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Submitter</th>
                  <th>Shape</th>
                  <th>State</th>
                  <th>Tasks</th>
                  <th>Submitted</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((j) => (
                  <tr key={j.id}>
                    <td>
                      <Link to={`/jobs/${j.id}`}>{j.name}</Link>
                    </td>
                    <td>{j.submitter}</td>
                    <td>{j.shape}</td>
                    <td>
                      <StateBadge state={j.state} />
                    </td>
                    <td>
                      <TaskCounts counts={j.tasks} />
                    </td>
                    <td>{fmtTime(j.created_at)}</td>
                    <td>
                      {canCancel(j) && (
                        <button
                          type="button"
                          className="secondary small"
                          disabled={cancel.isPending}
                          onClick={() => cancel.mutate(j.id)}
                        >
                          Cancel
                        </button>
                      )}
                    </td>
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

export function TaskCounts({ counts }: { counts: Record<string, number> }) {
  const order = ["succeeded", "running", "assigned", "pending", "failed", "cancelled"];
  return (
    <span className="counts">
      {order
        .filter((k) => counts[k])
        .map((k) => (
          <span key={k} className={`badge state-${k}`} title={k}>
            {counts[k]} {k}
          </span>
        ))}
      <span className="muted"> / {counts.total ?? 0}</span>
    </span>
  );
}

function SubmitJob({ group }: { group: Group }) {
  const qc = useQueryClient();
  const [text, setText] = useState(EXAMPLE_JOB);
  const parsed = useMemo(() => {
    try {
      return { doc: parseJobText(text, group.name), error: null };
    } catch (e) {
      return { doc: null, error: e instanceof JobParseError ? e : new JobParseError(String(e)) };
    }
  }, [text, group.name]);
  const submit = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/groups/{name}/jobs", {
          params: { path: { name: group.name } },
          body: parsed.doc as unknown as Record<string, unknown>,
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs", group.name] }),
  });
  return (
    <section className="card">
      <h2>Submit a job</h2>
      <p className="muted">
        Paste a job file (YAML or JSON, see plan §6.5). Images must be pinned by digest. The server checks the
        file again.
      </p>
      <label>
        Job file
        <textarea className="code" rows={14} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
      </label>
      {parsed.error && <div className="warn">{parsed.error.message}</div>}
      {parsed.doc && <p className="muted">{taskCount(parsed.doc)} task(s) will be created.</p>}
      <ErrorBox error={submit.error} />
      {submit.isSuccess && (
        <p className="ok">
          Submitted <Link to={`/jobs/${submit.data.id}`}>{submit.data.name}</Link> with {submit.data.tasks.total} task(s).
        </p>
      )}
      <button type="button" disabled={!parsed.doc || submit.isPending} onClick={() => submit.mutate()}>
        Submit job
      </button>
    </section>
  );
}
