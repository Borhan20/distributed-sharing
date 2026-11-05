import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";
import { creditKindText, creditsText } from "../../lib/credits";

/** Credits (docs/design/credits.md §4): earn by sharing your computer, spend by running jobs. */
export function CreditsTab({ group }: { group: Group }) {
  const qc = useQueryClient();
  const path = { name: group.name };
  const isCoordinator = group.my_role === "coordinator";
  const isManager = isCoordinator || group.my_role === "admin";
  const mine = useQuery({
    queryKey: ["credits", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/credits", { params: { path } })),
  });
  const history = useQuery({
    queryKey: ["credit-history", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/credits/history", { params: { path, query: { limit: 50 } } })),
    enabled: mine.data?.enabled === true,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["credits", group.name] });
    void qc.invalidateQueries({ queryKey: ["credit-history", group.name] });
    void qc.invalidateQueries({ queryKey: ["credit-requests", group.name] });
    void qc.invalidateQueries({ queryKey: ["credit-members", group.name] });
  };
  if (mine.isLoading) return <Loading />;
  if (mine.error || !mine.data) return <ErrorBox error={mine.error} />;
  const c = mine.data;
  if (!c.enabled) {
    return (
      <section className="card">
        <h2>Credits</h2>
        <p>
          This group does not use credits. With credits, members earn credits when their computer runs other people's
          work, and spend them when their work runs on other people's computers.
        </p>
        {isCoordinator && <p className="muted">Turn them on in the Settings tab.</p>}
      </section>
    );
  }
  return (
    <>
      <section className="card">
        <h2>Your credits</h2>
        <p className="big-number">{creditsText(c.balance)}</p>
        <p className="muted">
          {creditsText(c.earned)} earned · {creditsText(c.free)} free. Free credits (start credits and credits from the
          coordinator) are spent first and stay in this group. Earned credits can move to another of your groups.
        </p>
        <p className="muted small-text">
          One credit is one CPU for one hour. {c.rates.gib_hours_per_credit} GiB of memory for an hour is one credit, one
          GPU for an hour is {c.rates.gpu_hour}. Running on your own computer is free.
          {c.out_of_credits === "wait"
            ? " Out of credits, your jobs wait until you have credits again."
            : " Out of credits, your jobs still run, but only on computers nobody with credits is waiting for."}
        </p>
        <div className="row wrap">
          <GiveForm group={group} onDone={refresh} />
          <AskForm group={group} open={c.open_request ?? null} onDone={refresh} />
        </div>
      </section>
      {isCoordinator && <RequestsCard group={group} onDone={refresh} />}
      {isManager && <MembersCard group={group} isCoordinator={isCoordinator} onDone={refresh} />}
      <section className="card">
        <h2>History</h2>
        <ErrorBox error={history.error} />
        {history.data && history.data.length === 0 && <Empty>Nothing yet.</Empty>}
        {history.data && history.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>What</th>
                  <th>Credits</th>
                  <th>With</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((e) => (
                  <tr key={e.id}>
                    <td>{fmtTime(e.created_at)}</td>
                    <td>{creditKindText(e.kind)}</td>
                    <td className={e.amount < 0 ? "danger-text" : "ok"}>
                      {e.amount > 0 ? "+" : ""}
                      {creditsText(e.amount)}
                    </td>
                    <td>{e.counterparty ?? e.device ?? "–"}</td>
                    <td className="small-text">{e.note ?? "–"}</td>
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

function GiveForm({ group, onDone }: { group: Group; onDone: () => void }) {
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState(1);
  const give = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/groups/{name}/credits/give", { params: { path: { name: group.name } }, body: { to, amount } })),
    onSuccess: () => {
      setTo("");
      onDone();
    },
  });
  return (
    <form
      className="card-inset"
      onSubmit={(e) => {
        e.preventDefault();
        give.mutate();
      }}
    >
      <h3>Give credits to a member</h3>
      <label>
        Member
        <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="their user name" required />
      </label>
      <label>
        Credits
        <input type="number" min={0.01} step={0.01} value={amount} onChange={(e) => setAmount(Number(e.target.value))} />
      </label>
      <ErrorBox error={give.error} />
      {give.isSuccess && <p className="ok">Given.</p>}
      <button type="submit" disabled={give.isPending || !to}>
        Give
      </button>
    </form>
  );
}

function AskForm({
  group,
  open,
  onDone,
}: {
  group: Group;
  open: { amount: number; reason: string } | null;
  onDone: () => void;
}) {
  const [amount, setAmount] = useState(20);
  const [reason, setReason] = useState("");
  const ask = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/api/v1/groups/{name}/credits/requests", {
          params: { path: { name: group.name } },
          body: { amount, reason },
        }),
      ),
    onSuccess: () => {
      setReason("");
      onDone();
    },
  });
  if (open) {
    return (
      <div className="card-inset">
        <h3>Ask for credits</h3>
        <p>
          You asked for {creditsText(open.amount)} credits ({open.reason}). The coordinator has not answered yet.
        </p>
      </div>
    );
  }
  return (
    <form
      className="card-inset"
      onSubmit={(e) => {
        e.preventDefault();
        ask.mutate();
      }}
    >
      <h3>Ask the coordinator for credits</h3>
      <label>
        Credits
        <input type="number" min={1} step={1} value={amount} onChange={(e) => setAmount(Number(e.target.value))} />
      </label>
      <label>
        Why
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. thesis experiments" required />
      </label>
      <ErrorBox error={ask.error} />
      <button type="submit" disabled={ask.isPending || !reason.trim()}>
        Ask
      </button>
    </form>
  );
}

function RequestsCard({ group, onDone }: { group: Group; onDone: () => void }) {
  const path = { name: group.name };
  const requests = useQuery({
    queryKey: ["credit-requests", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/credits/requests", { params: { path, query: { state: "open" } } })),
  });
  const decide = useMutation({
    mutationFn: ({ id, approve, amount }: { id: string; approve: boolean; amount?: number }) =>
      approve
        ? unwrap(
            client.POST("/api/v1/groups/{name}/credits/requests/{request_id}/approve", {
              params: { path: { ...path, request_id: id } },
              body: amount !== undefined ? { amount } : {},
            }),
          )
        : unwrap(
            client.POST("/api/v1/groups/{name}/credits/requests/{request_id}/decline", {
              params: { path: { ...path, request_id: id } },
            }),
          ),
    onSuccess: onDone,
  });
  return (
    <section className="card">
      <h2>Requests for credits</h2>
      {requests.isLoading && <Loading />}
      <ErrorBox error={requests.error ?? decide.error} />
      {requests.data && requests.data.length === 0 && <Empty>No open requests.</Empty>}
      {requests.data && requests.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>Credits</th>
                <th>Why</th>
                <th>Asked</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {requests.data.map((r) => (
                <RequestRow key={r.id} r={r} busy={decide.isPending} decide={(approve, amount) => decide.mutate({ id: r.id, approve, amount })} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RequestRow({
  r,
  busy,
  decide,
}: {
  r: { member: string; amount: number; reason: string; created_at: string };
  busy: boolean;
  decide: (approve: boolean, amount?: number) => void;
}) {
  const [amount, setAmount] = useState(r.amount);
  return (
    <tr>
      <td>{r.member}</td>
      <td>
        <input
          type="number"
          min={0.01}
          max={r.amount}
          step={0.01}
          value={amount}
          onChange={(e) => setAmount(Number(e.target.value))}
          aria-label={`Credits for ${r.member}`}
        />{" "}
        <span className="muted small-text">of {creditsText(r.amount)}</span>
      </td>
      <td className="pre">{r.reason}</td>
      <td>{fmtTime(r.created_at)}</td>
      <td className="row">
        <button type="button" className="small" disabled={busy} onClick={() => decide(true, amount)}>
          Approve
        </button>
        <button type="button" className="secondary small" disabled={busy} onClick={() => decide(false)}>
          Decline
        </button>
      </td>
    </tr>
  );
}

function MembersCard({ group, isCoordinator, onDone }: { group: Group; isCoordinator: boolean; onDone: () => void }) {
  const path = { name: group.name };
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState(10);
  const [reason, setReason] = useState("");
  const members = useQuery({
    queryKey: ["credit-members", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/credits/members", { params: { path } })),
  });
  const grant = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/groups/{name}/credits/grant", { params: { path }, body: { to, amount, reason } })),
    onSuccess: () => {
      setReason("");
      onDone();
    },
  });
  return (
    <section className="card">
      <h2>Members' credits</h2>
      <ErrorBox error={members.error} />
      {members.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>Credits</th>
                <th>Earned</th>
                <th>Free</th>
              </tr>
            </thead>
            <tbody>
              {members.data.map((m) => (
                <tr key={m.member}>
                  <td>{m.member}</td>
                  <td>{creditsText(m.balance)}</td>
                  <td>{creditsText(m.earned)}</td>
                  <td>{creditsText(m.free)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {isCoordinator && (
        <form
          className="row wrap"
          onSubmit={(e) => {
            e.preventDefault();
            grant.mutate();
          }}
        >
          <label>
            Give free credits to
            <select value={to} onChange={(e) => setTo(e.target.value)} required>
              <option value="">a member…</option>
              {members.data?.map((m) => (
                <option key={m.member} value={m.member}>
                  {m.member}
                </option>
              ))}
            </select>
          </label>
          <label>
            Credits
            <input type="number" min={0.01} step={0.01} value={amount} onChange={(e) => setAmount(Number(e.target.value))} />
          </label>
          <label>
            Why
            <input value={reason} onChange={(e) => setReason(e.target.value)} required />
          </label>
          <button type="submit" disabled={grant.isPending || !to || !reason.trim()}>
            Give free credits
          </button>
          <ErrorBox error={grant.error} />
        </form>
      )}
    </section>
  );
}
