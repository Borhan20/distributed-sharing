import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type LogLine, type Task } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { ErrorBox, Loading, StateBadge } from "../components/ui";
import { TERMINAL, fmtTime } from "../lib/format";
import { TaskCounts } from "./group/JobsTab";

export function JobPage() {
  const { id = "" } = useParams();
  const { me } = useAuth();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const job = useQuery({
    queryKey: ["job", id],
    queryFn: () => unwrap(client.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: id } } })),
    refetchInterval: (q) => (q.state.data && TERMINAL.has(q.state.data.state) ? false : 3000),
  });
  const done = !!job.data && TERMINAL.has(job.data.state);
  const tasks = useQuery({
    queryKey: ["tasks", id],
    queryFn: () => unwrap(client.GET("/api/v1/jobs/{job_id}/tasks", { params: { path: { job_id: id } } })),
    refetchInterval: done ? false : 3000,
  });
  const cancel = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/jobs/{job_id}/cancel", { params: { path: { job_id: id } } })),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["job", id] });
      void qc.invalidateQueries({ queryKey: ["tasks", id] });
    },
  });
  if (job.isLoading) return <Loading />;
  if (!job.data) return <ErrorBox error={job.error ?? new Error("job not found")} />;
  const j = job.data;
  const spec = (j.spec?.spec ?? {}) as Record<string, unknown>;
  const selectedTask = tasks.data?.find((t) => t.index === selected) ?? null;
  return (
    <>
      <header className="page-head">
        <p className="muted">
          <Link to={`/groups/${j.group}`}>{j.group}</Link> / job
        </p>
        <h1>
          {j.name} <StateBadge state={j.state} />
        </h1>
        <p className="muted">
          Submitted by {j.submitter} on {fmtTime(j.created_at)} · shape {j.shape}
          {j.finished_at ? ` · finished ${fmtTime(j.finished_at)}` : ""}
        </p>
        <p>
          <TaskCounts counts={j.tasks} />
        </p>
        {!done && j.submitter === me?.handle && (
          <button type="button" className="secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
            Cancel job
          </button>
        )}
        <ErrorBox error={cancel.error} />
      </header>
      <section className="card">
        <h2>Spec</h2>
        <dl className="kv">
          <dt>Image</dt>
          <dd>
            <code className="break">{j.image}</code>
          </dd>
          <dt>Command</dt>
          <dd>
            <code className="break">{JSON.stringify(spec.command ?? [])}</code>
          </dd>
          <dt>Resources</dt>
          <dd>
            <code>{JSON.stringify(spec.resources ?? {})}</code>
          </dd>
          <dt>Isolation</dt>
          <dd>{(spec.isolation as { min_tier?: string } | undefined)?.min_tier ?? "T1"} or stronger</dd>
          <dt>Data class</dt>
          <dd>{String(spec.data_class ?? "internal")}</dd>
        </dl>
      </section>
      <section className="card">
        <h2>Tasks</h2>
        <ErrorBox error={tasks.error} />
        {tasks.data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Params</th>
                  <th>State</th>
                  <th>Epoch</th>
                  <th>Attempts</th>
                  <th>Failures</th>
                  <th>Device</th>
                  <th>Result</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {tasks.data.map((t: Task) => (
                  <tr key={t.id} className={t.index === selected ? "selected" : undefined}>
                    <td>{t.index}</td>
                    <td>
                      <code className="small-text">{Object.keys(t.params).length ? JSON.stringify(t.params) : "–"}</code>
                    </td>
                    <td>
                      <StateBadge state={t.state} />
                    </td>
                    <td>{t.epoch}</td>
                    <td>{t.attempts}</td>
                    <td>{t.failures}</td>
                    <td>{t.device ?? "–"}</td>
                    <td className="small-text">{resultText(t)}</td>
                    <td>
                      <button type="button" className="secondary small" onClick={() => setSelected(t.index)}>
                        Logs
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {selectedTask && <TaskLogs jobId={j.id} task={selectedTask} />}
    </>
  );
}

function resultText(t: Task): string {
  const r = t.result as Record<string, unknown> | null;
  if (!r) return "–";
  if (r.reason) return String(r.reason);
  if (r.exit_code !== undefined) return `exit ${r.exit_code}`;
  return "–";
}

/** Polls new log lines using the `after_id` cursor until the task ends. */
function TaskLogs({ jobId, task }: { jobId: string; task: Task }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);
  const live = !TERMINAL.has(task.state);
  const liveRef = useRef(live);
  liveRef.current = live;

  useEffect(() => {
    setLines([]);
    setError(null);
    cursor.current = 0;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const rows = await unwrap(
          client.GET("/api/v1/jobs/{job_id}/tasks/{index}/logs", {
            params: { path: { job_id: jobId, index: task.index }, query: { after_id: cursor.current, limit: 1000 } },
          }),
        );
        if (stopped) return;
        if (rows.length) {
          cursor.current = rows[rows.length - 1].id;
          setLines((prev) => [...prev, ...rows].slice(-5000));
        }
        setError(null);
        // Keep reading while lines arrive; poll slowly once the task has ended.
        if (rows.length === 1000) timer = setTimeout(tick, 0);
        else if (liveRef.current) timer = setTimeout(tick, 2000);
        else if (rows.length) timer = setTimeout(tick, 500);
      } catch (e) {
        if (stopped) return;
        setError(errorMessage(e));
        timer = setTimeout(tick, 5000);
      }
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, task.index]);

  let lastAttempt = "";
  return (
    <section className="card">
      <h2>
        Logs — task {task.index} {live && <span className="muted">(live)</span>}
      </h2>
      {error && <div className="error">{error}</div>}
      <pre className="logs" aria-live="polite">
        {lines.length === 0 && <span className="muted">No output yet.</span>}
        {lines.map((l) => {
          const header = l.attempt_id !== lastAttempt ? `── attempt ${l.attempt_id.slice(0, 8)} ──\n` : "";
          lastAttempt = l.attempt_id;
          return (
            <span key={l.id} className={`stream-${l.stream}`}>
              {header}
              {l.chunk}
            </span>
          );
        })}
      </pre>
    </section>
  );
}
