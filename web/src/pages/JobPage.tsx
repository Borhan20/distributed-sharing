import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type LogLine, type Task } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { ConfirmDialog, CopyButton, ErrorBox, Loading, RichText, StateBadge } from "../components/ui";
import { TERMINAL, fmtTime } from "../lib/format";
import { downloadFile } from "../lib/download";
import { commandText, splitImage } from "../lib/jobspec";
import { oversizeHint, requestedText, taskUsageText, usedText, type JobUsed, type Requested, type TaskUsage } from "../lib/usage";
import { TaskCounts } from "./group/JobsTab";
import type { RerunState } from "./group/SubmitJob";

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
  const asSubmitted = (j.spec?.as_submitted ?? {}) as { note?: string };
  const image = splitImage(j.image);
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
          {group.data?.state === "active" && ["coordinator", "admin", "member", "submitter"].includes(group.data?.my_role ?? "") && (
            <button
              type="button"
              className="secondary"
              onClick={() => navigate(`/groups/${j.group}?tab=jobs`, { state: { rerun: j.spec ?? undefined } satisfies RerunState })}
            >
              Run again with changes
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
      <p className="muted small-text">
        From your terminal: <code>pool attach {j.id}</code> follows this job and saves its files in{" "}
        <code>./pool-out</code>. <CopyButton text={`pool attach ${j.id}`} label="Copy" />
      </p>
      <section className="card">
        <h2>Spec</h2>
        <dl className="kv">
          <dt>Image</dt>
          <dd>
            <code className="break">{image.name}</code>
            {image.digest && (
              <span className="muted small-text" title={image.digest}>
                {" "}
                · fixed to {image.digest.slice(0, 19)}… when submitted
              </span>
            )}
          </dd>
          <dt>Command</dt>
          <dd>
            {Array.isArray(spec.command) && spec.command.length > 0 ? (
              <code className="break">{commandText(spec.command)}</code>
            ) : (
              <span className="muted">the image's own command</span>
            )}
          </dd>
          {asSubmitted.note && (
            <>
              <dt>Parts</dt>
              <dd>{asSubmitted.note}</dd>
            </>
          )}
          <dt>Each task</dt>
          <dd>
            {requestedText(j.requested as Requested | null) ?? resourcesText(spec.resources)}
            <SizingReasons sizing={j.sizing as Sizing | null} />
          </dd>
          {usedText(j.requested as Requested | null, j.used as JobUsed | null) && (
            <>
              <dt>Used</dt>
              <dd>
                {usedText(j.requested as Requested | null, j.used as JobUsed | null)}
                {oversizeHint(j.requested as Requested | null, j.used as JobUsed | null) && (
                  <div className="muted small-text">{oversizeHint(j.requested as Requested | null, j.used as JobUsed | null)}</div>
                )}
              </dd>
            </>
          )}
          <dt>Isolation</dt>
          <dd>{(spec.isolation as { min_tier?: string } | undefined)?.min_tier ?? "T1"} or stronger</dd>
          <dt>Data class</dt>
          <dd>{String(spec.data_class ?? "internal")}</dd>
        </dl>
      </section>
      <JobResults jobId={j.id} name={j.name} tasks={tasks.data ?? []} done={done} />
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
                  <th>Used</th>
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
                    <td className="small-text">{taskUsageText(t.usage as TaskUsage | null)}</td>
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

/**
 * All results in one place: one zip with every task's files and log, and the tasks' result.json files as one
 * table (docs/user-guide.md §7, "Collect the results").
 */
function JobResults({ jobId, name, tasks, done }: { jobId: string; name: string; tasks: Task[]; done: boolean }) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [soFar, setSoFar] = useState(0);
  const finished = tasks.filter((t) => t.state === "succeeded").length;
  const ran = tasks.some((t) => t.attempts > 0);
  // The table reads every task's result.json from the file store: load it once the job is done, or when asked.
  const table = useQuery({
    queryKey: ["results", jobId, done ? "done" : soFar],
    queryFn: () => unwrap(client.GET("/api/v1/jobs/{job_id}/results", { params: { path: { job_id: jobId } } })),
    enabled: finished > 0 && (done || soFar > 0),
    retry: false,
  });
  const noStore = table.error instanceof ApiError && table.error.status === 409;
  const save = (path: string, file: string) => {
    setError(null);
    setBusy(path);
    downloadFile(`/api/v1/jobs/${jobId}/${path}`, file)
      .catch(setError)
      .finally(() => setBusy(null));
  };
  if (!ran) return null;
  const rows = table.data?.rows ?? [];
  const shown = rows.slice(0, 200);
  return (
    <section className="card">
      <h2>Results</h2>
      <p className="muted">
        {done ? "The job has finished." : `${finished} of ${tasks.length} task(s) finished so far.`} Download all gives
        one zip: a folder per task with its files and log, <code>tasks.csv</code> (one line per task),{" "}
        <code>results.csv</code> (every <code>result.json</code> in one table) and CSV files of the same name merged.
      </p>
      {noStore && (
        <p className="notice">
          This server has no file store, so the files a task writes stay on the computer that ran it. Each task's log
          is still here: open it below.
        </p>
      )}
      {!noStore && (
        <div className="row wrap">
          <button type="button" disabled={busy !== null} onClick={() => save("results.zip", `${name}-results.zip`)}>
            {busy === "results.zip" ? "Preparing the zip…" : "Download all (zip)"}
          </button>
          {rows.length > 0 && (
            <button
              type="button"
              className="secondary"
              disabled={busy !== null}
              onClick={() => save("results.csv", `${name}-results.csv`)}
            >
              Download results table (CSV)
            </button>
          )}
          {!done && finished > 0 && (
            <button type="button" className="secondary" disabled={table.isFetching} onClick={() => setSoFar((n) => n + 1)}>
              {soFar ? "Refresh the results table" : "Show the results so far"}
            </button>
          )}
        </div>
      )}
      <ErrorBox error={error} />
      {table.error && !noStore ? <ErrorBox error={table.error} /> : null}
      {rows.length > 0 && table.data && (
        <>
          <h3>Results table</h3>
          <p className="muted small-text">
            From each task's <code>/out/result.json</code>.
            {table.data.missing > 0 && ` ${table.data.missing} task(s) have no result.json yet (not finished, failed, or none written).`}
            {rows.length > shown.length && ` Showing the first ${shown.length} of ${rows.length}; the CSV has all.`}
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {table.data.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={String(r.task)}>
                    {table.data.columns.map((c) => (
                      <td key={c}>{r[c] === undefined || r[c] === null ? "" : String(r[c])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {finished > 0 && table.data && rows.length === 0 && (
        <p className="muted small-text">
          No task wrote <code>/out/result.json</code>. Write one (a JSON object, e.g.{" "}
          <code>{'{"seed": 3, "score": 0.91}'}</code>) and the results show up here as one table.
        </p>
      )}
    </section>
  );
}

type Sizing = { auto?: string[]; first_run?: boolean; reasons?: string[] };

/** Why each size is what it is (docs/design/pool-decides.md §4.9). */
function SizingReasons({ sizing }: { sizing: Sizing | null }) {
  if (!sizing?.reasons?.length || !sizing.auto?.length) return null;
  return (
    <details className="small-text">
      <summary className="muted">Chosen by pool: why</summary>
      <ul>
        {sizing.reasons.map((r, i) => (
          <li key={i}>{r}</li>
        ))}
      </ul>
    </details>
  );
}

function resourcesText(r: unknown): string {
  const res = (r ?? {}) as { cpu?: number; memory?: string; disk?: string; gpu?: { count?: number } | null };
  const parts = [`${res.cpu ?? 1} CPU`, `${res.memory ?? "1Gi"} memory`, `${res.disk ?? "1Gi"} disk`];
  if (res.gpu?.count) parts.push(`${res.gpu.count} GPU`);
  return parts.join(", ");
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
      <div className="row between">
        <h2>
          Logs — task {task.index} {live && <span className="muted">(live)</span>}
        </h2>
        <button
          type="button"
          className="secondary small"
          onClick={() =>
            downloadFile(`/api/v1/jobs/${jobId}/tasks/${task.index}/log.txt`, `task-${task.index}-log.txt`).catch((e) =>
              setError(errorMessage(e)),
            )
          }
        >
          Download log
        </button>
      </div>
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
