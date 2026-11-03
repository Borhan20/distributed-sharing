import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group, type Job } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Empty, ErrorBox, Loading, StateBadge } from "../../components/ui";
import { fmtTime } from "../../lib/format";
import { SubmitJob } from "./SubmitJob";

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
