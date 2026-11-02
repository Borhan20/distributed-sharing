import type { AuditEvent } from "../api/client";
import { fmtTime } from "../lib/format";

export function AuditTable({ rows }: { rows: AuditEvent[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Time</th>
            <th>Actor</th>
            <th>Action</th>
            <th>Target</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id}>
              <td>{e.id}</td>
              <td>{fmtTime(e.ts)}</td>
              <td>
                {e.actor ?? "system"} <span className="muted">({e.actor_type})</span>
              </td>
              <td>
                <code>{e.action}</code>
              </td>
              <td className="muted">
                {e.target_type ? `${e.target_type} ${e.target_id?.slice(0, 8) ?? ""}` : "–"}
              </td>
              <td>
                <code className="small-text">{Object.keys(e.details).length ? JSON.stringify(e.details) : ""}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
