import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { client, unwrap, type Group } from "../../api/client";
import { AuditTable } from "../../components/AuditTable";
import { Empty, ErrorBox, Loading } from "../../components/ui";
import { downloadJson } from "../../lib/download";

export function AuditTab({ group }: { group: Group }) {
  const [exportError, setExportError] = useState<unknown>(null);
  const audit = useQuery({
    queryKey: ["audit", group.name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}/audit", { params: { path: { name: group.name }, query: { limit: 500 } } })),
  });
  return (
    <section className="card">
      <div className="row between">
        <h2>Audit log</h2>
        <button
          type="button"
          className="secondary"
          onClick={() => {
            setExportError(null);
            downloadJson(`/api/v1/groups/${encodeURIComponent(group.name)}/audit/export`, `audit-${group.name}.json`).catch(setExportError);
          }}
        >
          Export JSON
        </button>
      </div>
      <p className="muted">Append-only and hash-chained. Showing the oldest 500 events.</p>
      <ErrorBox error={exportError} />
      {audit.isLoading && <Loading />}
      <ErrorBox error={audit.error} />
      {audit.data && audit.data.length === 0 && <Empty>No events.</Empty>}
      {audit.data && audit.data.length > 0 && <AuditTable rows={audit.data} />}
    </section>
  );
}
