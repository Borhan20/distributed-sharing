import { useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { ErrorBox, Loading, StateBadge, Tabs } from "../components/ui";
import { fmtTime } from "../lib/format";
import { JobsTab } from "./group/JobsTab";
import { MembersTab } from "./group/MembersTab";
import { InvitesTab } from "./group/InvitesTab";
import { JoinRequestsTab } from "./group/JoinRequestsTab";
import { DevicesTab, UsageTab } from "./group/DevicesTab";
import { AuditTab } from "./group/AuditTab";
import { SettingsTab } from "./group/SettingsTab";

type TabId = "jobs" | "members" | "invites" | "requests" | "devices" | "usage" | "audit" | "settings";

const MANAGERS = new Set(["coordinator", "admin"]);
const VOTERS = new Set(["coordinator", "admin", "member"]);

export function GroupPage() {
  const { name = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const group = useQuery({
    queryKey: ["group", name],
    queryFn: () => unwrap(client.GET("/api/v1/groups/{name}", { params: { path: { name } } })),
  });
  if (group.isLoading) return <Loading />;
  if (group.error || !group.data) return <ErrorBox error={group.error ?? new Error("group not found")} />;
  const g = group.data;
  const role = g.my_role ?? "";
  const tabs: { id: TabId; label: string }[] = [
    { id: "jobs", label: "Jobs" },
    { id: "members", label: "Members" },
  ];
  if (MANAGERS.has(role)) tabs.push({ id: "invites", label: "Invites" });
  if (MANAGERS.has(role) || (VOTERS.has(role) && g.approval_rule !== "coordinator-only")) {
    tabs.push({ id: "requests", label: "Join requests" });
  }
  tabs.push({ id: "devices", label: "Devices" }, { id: "usage", label: "Usage" });
  if (MANAGERS.has(role)) tabs.push({ id: "audit", label: "Audit log" });
  if (role === "coordinator") tabs.push({ id: "settings", label: "Settings" });
  const requested = params.get("tab") as TabId | null;
  const active: TabId = requested && tabs.some((t) => t.id === requested) ? requested : "jobs";

  return (
    <>
      <header className="page-head">
        <h1>
          {g.display_name} <span className="muted">({g.name})</span>
        </h1>
        <p className="muted">
          Your role: <strong>{role}</strong> · Approval: {g.approval_rule}
          {g.approval_rule === "any-k-members" ? ` (k=${g.approval_k})` : ""} · Probation: {g.probation_days} days ·
          Retention: {g.retention_days} days · Expires: {g.expires_at ? fmtTime(g.expires_at) : "never"} ·{" "}
          <StateBadge state={g.state} />
        </p>
      </header>
      <Tabs tabs={tabs} active={active} onChange={(id) => setParams({ tab: id }, { replace: true })} />
      <div className="tab-body">
        {active === "jobs" && <JobsTab group={g} />}
        {active === "members" && <MembersTab group={g} />}
        {active === "invites" && <InvitesTab group={g} />}
        {active === "requests" && <JoinRequestsTab group={g} />}
        {active === "devices" && <DevicesTab group={g} />}
        {active === "usage" && <UsageTab group={g} />}
        {active === "audit" && <AuditTab group={g} />}
        {active === "settings" && <SettingsTab group={g} />}
      </div>
    </>
  );
}
