import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtTime } from "../../lib/format";

export function JoinRequestsTab({ group }: { group: Group }) {
  const qc = useQueryClient();
  const path = { name: group.name };
  const requests = useQuery({
    queryKey: ["join-requests", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/join-requests", { params: { path, query: { state: "pending" } } })),
  });
  const vote = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      unwrap(
        client.POST("/api/v1/groups/{name}/join-requests/{request_id}/votes", {
          params: { path: { ...path, request_id: id } },
          body: { decision },
        }),
      ),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["join-requests", group.name] });
      void qc.invalidateQueries({ queryKey: ["members", group.name] });
    },
  });
  return (
    <section className="card">
      <h2>Pending join requests</h2>
      <p className="muted">
        Rule: {group.approval_rule}
        {group.approval_rule === "any-k-members" ? ` — ${group.approval_k} approvals needed` : ""}
        {group.approval_rule === "majority" ? " — more than half of coordinators, admins and members" : ""}
      </p>
      {requests.isLoading && <Loading />}
      <ErrorBox error={requests.error ?? vote.error} />
      {requests.data && requests.data.length === 0 && <Empty>No pending requests.</Empty>}
      {requests.data && requests.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Who</th>
                <th>Message</th>
                <th>Votes</th>
                <th>Asked</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {requests.data.map((r) => (
                <tr key={r.id}>
                  <td>{r.handle}</td>
                  <td className="pre">{r.message || <span className="muted">–</span>}</td>
                  <td>
                    {r.approvals} approve · {r.rejections} reject · {r.needed} needed
                  </td>
                  <td>{fmtTime(r.created_at)}</td>
                  <td className="row">
                    <button type="button" className="small" disabled={vote.isPending} onClick={() => vote.mutate({ id: r.id, decision: "approve" })}>
                      Approve
                    </button>
                    <button type="button" className="danger small" disabled={vote.isPending} onClick={() => vote.mutate({ id: r.id, decision: "reject" })}>
                      Reject
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
