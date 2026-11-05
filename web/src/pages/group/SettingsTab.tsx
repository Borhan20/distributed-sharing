import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { ConfirmDialog, ErrorBox } from "../../components/ui";

type Rule = "coordinator-only" | "any-k-members" | "majority";

export function SettingsTab({ group }: { group: Group }) {
  const qc = useQueryClient();
  const path = { name: group.name };
  const [rule, setRule] = useState<Rule>(group.approval_rule as Rule);
  const [k, setK] = useState(group.approval_k);
  const [probation, setProbation] = useState(group.probation_days);
  const [retention, setRetention] = useState(group.retention_days);
  // Hours, as people think of a time limit; the API takes seconds.
  const [firstRunHours, setFirstRunHours] = useState((group.default_time_limit_s ?? 14400) / 3600);
  const [closing, setClosing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PATCH("/api/v1/groups/{name}", {
          params: { path },
          body: {
            approval_rule: rule,
            approval_k: k,
            probation_days: probation,
            retention_days: retention,
            default_time_limit_s: Math.round(firstRunHours * 3600),
          },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["group", group.name] }),
  });
  const close = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/groups/{name}/close", { params: { path }, body: { confirm: true } })),
    onSuccess: () => {
      setClosing(false);
      void qc.invalidateQueries({ queryKey: ["group", group.name] });
      void qc.invalidateQueries({ queryKey: ["groups"] });
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      unwrap(client.DELETE("/api/v1/groups/{name}", { params: { path }, body: { confirm_name: group.name } })),
    onSuccess: () => {
      setDeleting(false);
      void qc.invalidateQueries({ queryKey: ["groups"] });
      void qc.invalidateQueries({ queryKey: ["deleted-groups"] });
      navigate("/");
    },
  });
  const active = group.state === "active";
  return (
    <>
      <section className="card">
        <h2>Settings</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <label>
            Approval rule for join requests
            <select value={rule} onChange={(e) => setRule(e.target.value as Rule)} disabled={!active}>
              <option value="coordinator-only">coordinator-only</option>
              <option value="any-k-members">any-k-members</option>
              <option value="majority">majority</option>
            </select>
          </label>
          {rule === "any-k-members" && (
            <label>
              Approvals needed (k)
              <input type="number" min={1} max={100} value={k} onChange={(e) => setK(Number(e.target.value))} disabled={!active} />
            </label>
          )}
          <label>
            Probation for new members (days, 0 = off)
            <input type="number" min={0} max={365} value={probation} onChange={(e) => setProbation(Number(e.target.value))} disabled={!active} />
          </label>
          <label>
            Data retention after the group ends (days)
            <input type="number" min={1} max={3650} value={retention} onChange={(e) => setRetention(Number(e.target.value))} disabled={!active} />
          </label>
          <label>
            Time limit of a first run (hours)
            <input
              type="number"
              min={0.25}
              max={168}
              step={0.25}
              value={firstRunHours}
              onChange={(e) => setFirstRunHours(Number(e.target.value))}
              disabled={!active}
            />
          </label>
          <p className="muted small-text">
            When someone runs an image for the first time and sets no time limit, each task may run this long. After
            that, pool sets the limit to 3 × their longest run.
          </p>
          <ErrorBox error={save.error} />
          {save.isSuccess && <p className="ok">Saved.</p>}
          <button type="submit" disabled={!active || save.isPending}>
            Save settings
          </button>
        </form>
      </section>
      <section className="card danger-zone">
        <h2>Close the group</h2>
        <p>
          All queued and running jobs stop at once. Members can still see the group and its results. To remove the
          group and its data, delete it below.
        </p>
        <button type="button" className="danger" disabled={!active} onClick={() => setClosing(true)}>
          Close group…
        </button>
      </section>
      <ConfirmDialog
        open={closing}
        title={`Close ${group.display_name}?`}
        confirmLabel="Close group"
        typeToConfirm={group.name}
        busy={close.isPending}
        error={close.error}
        onCancel={() => {
          setClosing(false);
          close.reset();
        }}
        onConfirm={() => close.mutate()}
      >
        <p>This cannot be undone from the UI. Every job in the group is cancelled.</p>
      </ConfirmDialog>
      <section className="card danger-zone">
        <h2>Delete the group</h2>
        <p>
          The group disappears for everyone at once and its work stops. For 7 days you, or a server admin, can restore
          it from the Groups page. After that its jobs, logs and files are deleted for good. The audit log and each
          computer owner's "what ran here" list are kept.
        </p>
        <button type="button" className="danger" onClick={() => setDeleting(true)}>
          Delete group…
        </button>
      </section>
      <ConfirmDialog
        open={deleting}
        title={`Delete ${group.display_name}?`}
        confirmLabel="Delete group"
        typeToConfirm={group.name}
        busy={remove.isPending}
        error={remove.error}
        onCancel={() => {
          setDeleting(false);
          remove.reset();
        }}
        onConfirm={() => remove.mutate()}
      >
        <p>Running jobs stop now. You can restore the group for 7 days.</p>
      </ConfirmDialog>
    </>
  );
}
