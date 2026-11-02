import { useState } from "react";
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
  const [closing, setClosing] = useState(false);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PATCH("/api/v1/groups/{name}", {
          params: { path },
          body: { approval_rule: rule, approval_k: k, probation_days: probation, retention_days: retention },
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
          All queued and running jobs stop at once. Data is kept for {group.retention_days} days; deleting it is a
          separate step.
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
    </>
  );
}
