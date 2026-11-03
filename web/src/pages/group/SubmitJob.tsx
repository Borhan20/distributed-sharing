import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { ErrorBox, Tabs } from "../../components/ui";
import {
  EXAMPLE_FORM,
  JobParseError,
  SIZES,
  formProblems,
  formTaskCount,
  formToJob,
  formToYaml,
  jobToForm,
  parseJobText,
  taskCount,
  type JobForm,
  type RunMode,
  type Size,
} from "../../lib/jobspec";

type View = "form" | "yaml";

/** What "Run again with changes" on a job page passes along (router state). */
export type RerunState = { rerun?: Record<string, unknown> };

/**
 * Submit a job: a form by default (image, command, how to run, size), or a pasted job file.
 * docs/user-guide.md §3 explains the fields.
 */
export function SubmitJob({ group }: { group: Group }) {
  const qc = useQueryClient();
  const location = useLocation();
  const rerun = (location.state as RerunState | null)?.rerun;
  const [view, setView] = useState<View>("form");
  const [form, setForm] = useState<JobForm>(() => {
    try {
      return rerun ? jobToForm(rerun) : EXAMPLE_FORM;
    } catch {
      return EXAMPLE_FORM;
    }
  });
  const [text, setText] = useState("");
  const top = useRef<HTMLElement>(null);
  useEffect(() => {
    if (rerun) top.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [rerun]);

  const set = <K extends keyof JobForm>(key: K, value: JobForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const problems = useMemo(() => formProblems(form), [form]);
  const parsed = useMemo(() => {
    if (view !== "yaml") return { doc: null, error: null };
    try {
      return { doc: parseJobText(text, group.name), error: null };
    } catch (e) {
      return { doc: null, error: e instanceof JobParseError ? e : new JobParseError(String(e)) };
    }
  }, [view, text, group.name]);

  const submit = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      unwrap(client.POST("/api/v1/groups/{name}/jobs", { params: { path: { name: group.name } }, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs", group.name] }),
  });

  const switchTo = (next: View) => {
    if (next === "yaml") setText(formToYaml(form));
    if (next === "form" && parsed.doc) {
      try {
        setForm(jobToForm(parsed.doc));
      } catch {
        /* keep the form as it was */
      }
    }
    setView(next);
  };

  const count = view === "form" ? formTaskCount(form) : parsed.doc ? taskCount(parsed.doc) : 0;
  const ready = view === "form" ? problems.length === 0 : !!parsed.doc;

  return (
    <section className="card" ref={top}>
      <h2>{rerun ? "Run again with changes" : "Submit a job"}</h2>
      <Tabs
        tabs={[
          { id: "form", label: "Form" },
          { id: "yaml", label: "Job file (YAML)" },
        ]}
        active={view}
        onChange={switchTo}
      />
      {view === "form" ? (
        <JobFormFields form={form} set={set} />
      ) : (
        <>
          <p className="muted">
            Paste a job file: the short form shown here, or the full form (apiVersion, metadata, spec). The command
            line takes the same file: <code>pool submit job.yaml --group {group.name}</code>.
          </p>
          <label>
            Job file
            <textarea className="code" rows={16} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
          </label>
          {parsed.error && <div className="warn">{parsed.error.message}</div>}
        </>
      )}

      {view === "form" && problems.length > 0 && (
        <ul className="warn plain">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {ready && (
        <p className="muted">
          {count === null
            ? "Auto: when you submit, the server splits the job into as many parts as the group's computers can run at once."
            : `${count} task(s) will run.`}
        </p>
      )}
      <ErrorBox error={submit.error} />
      {submit.isSuccess && (
        <p className="ok">
          Submitted <Link to={`/jobs/${submit.data.id}`}>{submit.data.name}</Link> with {submit.data.tasks.total}{" "}
          task(s).
        </p>
      )}
      <div className="row">
        <button
          type="button"
          disabled={!ready || submit.isPending}
          onClick={() => submit.mutate(view === "form" ? formToJob(form) : (parsed.doc as Record<string, unknown>))}
        >
          {submit.isPending ? "Submitting…" : "Submit job"}
        </button>
        {view === "form" && (
          <button type="button" className="secondary" onClick={() => switchTo("yaml")}>
            Show as job file
          </button>
        )}
      </div>
    </section>
  );
}

function JobFormFields({ form, set }: { form: JobForm; set: <K extends keyof JobForm>(k: K, v: JobForm[K]) => void }) {
  const modes: { id: RunMode; label: string; hint: string }[] = [
    { id: "once", label: "Once", hint: "One task, on one computer big enough for it." },
    {
      id: "parts",
      label: "Split into parts",
      hint: "Copies of the same command. Each part gets its number: {{index}} in the command, POOL_TASK_INDEX inside the container (0, 1, 2, …), and {{count}} / POOL_TASK_COUNT.",
    },
    { id: "sweep", label: "For each value", hint: "One task for each combination of values, e.g. seed 0..4." },
  ];
  return (
    <>
      <label>
        Image
        <input
          value={form.image}
          onChange={(e) => set("image", e.target.value)}
          placeholder="ghcr.io/your-org/app:1"
          spellCheck={false}
          autoCapitalize="off"
        />
        <span className="muted small-text">
          A name with a tag, as you push it, e.g. <code>ghcr.io/your-org/app:1</code>. The server fixes the exact version
          when you submit, so every task runs the same image. Private image? Add your read-only token under{" "}
          <Link to="/images">My images</Link>.
        </span>
      </label>
      <label>
        Command
        <input
          className="code"
          value={form.command}
          onChange={(e) => set("command", e.target.value)}
          placeholder="python /app/main.py --part {{index}}"
          spellCheck={false}
          autoCapitalize="off"
        />
        <span className="muted small-text">
          As you would type it in a terminal. Leave it empty to run the image's own command. Write result files to{" "}
          <code>/out</code>.
        </span>
      </label>

      <fieldset className="choices">
        <legend>How to run</legend>
        {modes.map((m) => (
          <label key={m.id} className="check">
            <input type="radio" name="mode" checked={form.mode === m.id} onChange={() => set("mode", m.id)} />
            <span>
              <strong>{m.label}</strong> <span className="muted small-text">{m.hint}</span>
            </span>
          </label>
        ))}
        {form.mode === "parts" && (
          <div className="row wrap indent">
            <label className="check">
              <input type="checkbox" checked={form.parts === "auto"} onChange={(e) => set("parts", e.target.checked ? "auto" : 4)} />
              Auto: as many as the group's computers can run at once
            </label>
            {form.parts !== "auto" && (
              <label>
                Parts
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={form.parts}
                  onChange={(e) => set("parts", Math.trunc(Number(e.target.value)))}
                />
              </label>
            )}
          </div>
        )}
        {form.mode === "sweep" && (
          <div className="indent">
            {form.sweep.map((row, i) => (
              <div className="row wrap" key={i}>
                <label>
                  Name
                  <input
                    value={row.name}
                    onChange={(e) => set("sweep", form.sweep.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
                    placeholder="seed"
                    spellCheck={false}
                  />
                </label>
                <label className="grow">
                  Values
                  <input
                    value={row.values}
                    onChange={(e) =>
                      set("sweep", form.sweep.map((r, j) => (j === i ? { ...r, values: e.target.value } : r)))
                    }
                    placeholder="0..9   or   small, large"
                    spellCheck={false}
                  />
                </label>
                <button
                  type="button"
                  className="secondary small"
                  onClick={() => set("sweep", form.sweep.filter((_, j) => j !== i))}
                  aria-label={`Remove ${row.name || "this value"}`}
                >
                  Remove
                </button>
              </div>
            ))}
            <button
              type="button"
              className="secondary small"
              onClick={() => set("sweep", [...form.sweep, { name: "", values: "" }])}
            >
              Add a value to sweep
            </button>
            <p className="muted small-text">
              Use each name in the command, e.g. <code>--seed {"{{seed}}"}</code>. Several names multiply: 5 seeds × 2
              sizes = 10 tasks.
            </p>
          </div>
        )}
      </fieldset>

      <div className="row wrap">
        <label>
          Size of each task
          <select value={form.size} onChange={(e) => set("size", e.target.value as Size)}>
            {Object.entries(SIZES).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
            <option value="custom">Custom</option>
          </select>
        </label>
        <label>
          Time limit per task
          <input value={form.timeLimit} onChange={(e) => set("timeLimit", e.target.value)} placeholder="30m, 2h" />
        </label>
      </div>
      {form.size === "custom" && (
        <div className="row wrap">
          <label>
            CPU cores
            <input type="number" min={0.1} step={0.5} value={form.cpu} onChange={(e) => set("cpu", Number(e.target.value))} />
          </label>
          <label>
            Memory
            <input value={form.memory} onChange={(e) => set("memory", e.target.value)} placeholder="512Mi, 4Gi" />
          </label>
          <label>
            Disk
            <input value={form.disk} onChange={(e) => set("disk", e.target.value)} placeholder="1Gi" />
          </label>
        </div>
      )}
      <details>
        <summary>More options</summary>
        <div className="row wrap">
          <label>
            Name
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="from the image" />
          </label>
          <label>
            Retries after a failure
            <input
              type="number"
              min={0}
              max={20}
              value={form.retries}
              onChange={(e) => set("retries", Math.trunc(Number(e.target.value)))}
            />
          </label>
          <label>
            GPUs per task
            <input type="number" min={0} max={8} value={form.gpu} onChange={(e) => set("gpu", Math.trunc(Number(e.target.value)))} />
          </label>
        </div>
      </details>
    </>
  );
}
