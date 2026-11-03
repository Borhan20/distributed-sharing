import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type LogLine, type Task } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { ConfirmDialog, ErrorBox, Loading, RichText, StateBadge } from "../components/ui";
import { TERMINAL, fmtTime } from "../lib/format";
import { TaskCounts } from "./group/JobsTab";

export function JobPage() {
  const { id = "" } = useParams();
  const { me } = useAuth();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();
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
  const groupName = job.data?.group;
  const group = useQuery({
    queryKey: ["group", groupName],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}", { params: { path: { name: groupName ?? "" } } })),
    enabled: !!groupName,
  });
  const remove = useMutation({
    mutationFn: () => unwrap(client.DELETE("/api/v1/jobs/{job_id}", { params: { path: { job_id: id } } })),
    onSuccess: () => {
      setDeleting(false);
      void qc.invalidateQueries({ queryKey: ["jobs", groupName] });
      navigate(`/groups/${groupName}`);
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
        <div className="row">
          {!done && j.submitter === me?.handle && (
            <button type="button" className="secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
              Cancel job
            </button>
          )}
          {(j.submitter === me?.handle || ["coordinator", "admin"].includes(group.data?.my_role ?? "")) && (
            <button type="button" className="danger" onClick={() => setDeleting(true)}>
              Delete job…
            </button>
          )}
        </div>
        <ErrorBox error={cancel.error} />
        <ConfirmDialog
          open={deleting}
          title={`Delete ${j.name}?`}
          confirmLabel="Delete job"
          busy={remove.isPending}
          error={remove.error}
          onCancel={() => {
            setDeleting(false);
            remove.reset();
          }}
          onConfirm={() => remove.mutate()}
        >
          <p>
            {done ? "" : "Its running tasks stop first. "}Its tasks, logs and output files are deleted for good. The audit
            log and each computer owner's "what ran here" list keep a short record.
          </p>
        </ConfirmDialog>
      </header>
      {!done && <WaitingReasons jobId={id} />}
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
      {selectedTask && <TaskOutputs jobId={j.id} task={selectedTask} />}
      {selectedTask && <TaskLogs jobId={j.id} task={selectedTask} />}
    </>
  );
}

function resultText(t: Task): string {
  const r = t.result as Record<string, unknown> | null;
  if (!r) return "–";
  if (r.error) return `${String(r.error)}${r.exit_code !== undefined ? ` (exit ${r.exit_code})` : ""}`;
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

/** Why tasks are still waiting, in the server's plain words (GET /jobs/{id}/waiting). */
function WaitingReasons({ jobId }: { jobId: string }) {
  const waiting = useQuery({
    queryKey: ["waiting", jobId],
    queryFn: () => unwrap(client.GET("/api/v1/jobs/{job_id}/waiting", { params: { path: { job_id: jobId } } })),
    refetchInterval: 5000,
  });
  const reasons = waiting.data?.reasons ?? [];
  if (reasons.length === 0) return null;
  const n = waiting.data?.pending_tasks ?? 0;
  return (
    <section className="card waiting" aria-live="polite">
      <h2>
        {n} task{n === 1 ? "" : "s"} waiting: why
      </h2>
      <ul>
        {reasons.map((r) => (
          <li key={r.code}>
            <RichText text={r.message} />
            {r.hint && (
              <span className="hint">
                <RichText text={r.hint} />
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

type Output = { path: string; sha256: string; size: number };

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

/** Files the task wrote to /out. Downloading asks the server for a short-lived link to the file store. */
function TaskOutputs({ jobId, task }: { jobId: string; task: Task }) {
  const outputs = (((task.result ?? {}) as { outputs?: Output[] }).outputs ?? []) as Output[];
  const download = useMutation({
    mutationFn: (path: string) =>
      unwrap(
        client.GET("/api/v1/jobs/{job_id}/tasks/{index}/download", {
          params: { path: { job_id: jobId, index: task.index }, query: { path } },
        }),
      ),
    onSuccess: (link) => window.location.assign(link.url),
  });
  if (task.state !== "succeeded" || outputs.length === 0) return null;
  return (
    <section className="card">
      <h2>Output files — task {task.index}</h2>
      <ul className="plain">
        {outputs.map((o) => (
          <li key={o.path} className="row between">
            <span>
              <code>{o.path}</code> <span className="muted small-text">{fmtBytes(o.size)} · sha256 {o.sha256.slice(0, 12)}…</span>
            </span>
            <button type="button" className="small" disabled={download.isPending} onClick={() => download.mutate(o.path)}>
              Download
            </button>
          </li>
        ))}
      </ul>
      <ErrorBox error={download.error} />
      <p className="muted small-text">
        Command line: <code>pool download {jobId} --task {task.index}</code>
      </p>
    </section>
  );
}
