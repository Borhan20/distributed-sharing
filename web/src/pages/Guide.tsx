import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { client, unwrap } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { EXAMPLE_FORM, formToYaml } from "../lib/jobspec";

/**
 * In-app user guide, one section per role (plan §6.2 roles). It must match what the
 * server enforces (control-plane/pool_cp/authz) and the screens' real labels.
 * The long walkthrough with examples is docs/user-guide.md.
 */

type Role = "coordinator" | "admin" | "member" | "contributor" | "submitter";

const ROLE_INFO: { id: Role; title: string; summary: string }[] = [
  { id: "coordinator", title: "Coordinator", summary: "Runs the group: settings, roles, members, closing it." },
  { id: "admin", title: "Admin", summary: "Helps run the group: invites, join requests, audit log." },
  { id: "member", title: "Member", summary: "Submits jobs and shares computers." },
  { id: "contributor", title: "Contributor", summary: "Shares computers only (e.g. a lab machine)." },
  { id: "submitter", title: "Submitter", summary: "Submits jobs only (e.g. a student without a spare computer)." },
];

const Y = "✓";
const PERMISSIONS: [string, Role[]][] = [
  ["See the group's jobs, members, computers and usage", ["coordinator", "admin", "member", "contributor", "submitter"]],
  ["Submit jobs", ["coordinator", "admin", "member", "submitter"]],
  ["Share a computer with the group", ["coordinator", "admin", "member", "contributor"]],
  ["Vote on join requests (when the group's rule asks members)", ["coordinator", "admin", "member"]],
  ["Create invite links, see the audit log, cancel or delete anyone's job", ["coordinator", "admin"]],
  ["Remove a computer from the group, and let it back in", ["coordinator", "admin"]],
  ["Change settings and roles, remove members, close, delete or restore the group", ["coordinator"]],
];

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="card" id={id} aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`}>{title}</h2>
      {children}
    </section>
  );
}

export function GuidePage() {
  const { token, me } = useAuth();
  const groups = useQuery({
    queryKey: ["groups"],
    queryFn: () => unwrap(client.GET("/api/v1/groups")),
    enabled: !!token,
  });
  const mine = new Map<Role, string[]>();
  for (const g of groups.data ?? []) {
    const r = g.my_role as Role | null | undefined;
    if (r) mine.set(r, [...(mine.get(r) ?? []), g.name]);
  }
  const hasAny = (roles: Role[]) => roles.some((r) => mine.has(r));

  return (
    <div className="guide">
      <h1>User guide</h1>
      <p>
        pool lets a group of people share their computers to run each other's jobs. Every job runs in a locked-down
        container, and each computer's owner decides what to share and can stop everything at once.
      </p>
      {token && (
        <p className="muted">
          {mine.size > 0
            ? `Your roles: ${[...mine].map(([r, gs]) => `${r} in ${gs.join(", ")}`).join("; ")}.`
            : "You are not in a group yet: start with “Getting started”."}
          {me?.server_admin ? " You are also a server admin." : ""}
        </p>
      )}
      <nav className="guide-nav" aria-label="Guide sections">
        <a href="#start">Getting started</a>
        <a href="#run-jobs" className={hasAny(["coordinator", "admin", "member", "submitter"]) ? "mine" : ""}>
          Run jobs
        </a>
        <a href="#share" className={hasAny(["coordinator", "admin", "member", "contributor"]) ? "mine" : ""}>
          Share a computer
        </a>
        <a href="#coordinator" className={mine.has("coordinator") ? "mine" : ""}>
          Coordinators
        </a>
        <a href="#admin" className={mine.has("admin") ? "mine" : ""}>
          Admins
        </a>
        <a href="#server-admin" className={me?.server_admin ? "mine" : ""}>
          Server admins
        </a>
        <a href="#help">When something is wrong</a>
      </nav>

      <Section id="roles" title="Roles: who can do what">
        <p className="muted">Each person has one role per group. The coordinator chooses it when inviting or later.</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Can…</th>
                {ROLE_INFO.map((r) => (
                  <th key={r.id} title={r.summary}>
                    {r.title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {PERMISSIONS.map(([what, roles]) => (
                <tr key={what}>
                  <td>{what}</td>
                  {ROLE_INFO.map((r) => (
                    <td key={r.id} aria-label={roles.includes(r.id) ? "yes" : "no"}>
                      {roles.includes(r.id) ? Y : "–"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul>
          {ROLE_INFO.map((r) => (
            <li key={r.id}>
              <strong>{r.title}:</strong> {r.summary}
            </li>
          ))}
        </ul>
      </Section>

      <Section id="start" title="Getting started (everyone)">
        <ol className="steps">
          <li>
            <strong>Join a group.</strong> Open the invite link someone sent you, sign in, and press <em>Accept invite</em>.
            No link? On <Link to="/">Groups</Link>, use <em>Ask to join a group</em> with the group's short name; the
            group approves you by its own rule.
          </li>
          <li>
            <strong>Or start your own group.</strong> On <Link to="/">Groups</Link>, use <em>Create a group</em> and pick a
            template (research team, student group, course, hackathon…). You become its coordinator.
          </li>
          <li>
            <strong>Make sure the group has a computer.</strong> Jobs run on computers that members share. If nobody has
            shared one yet, see <a href="#share">Share a computer</a>.
          </li>
        </ol>
      </Section>

      <Section id="run-jobs" title="Run jobs (members and submitters)">
        <ol className="steps">
          <li>
            Open your group, tab <em>Jobs</em>, then <em>Submit a job</em>. The form starts with a small example that runs
            as-is: submit it once to see the whole flow.
          </li>
          <li>
            Change the example to your work:
            <ul>
              <li>
                <strong>Image</strong>: your image as you push it, e.g. <code>ghcr.io/your-org/app:1</code> or{" "}
                <code>yourname/app:v2</code>. The server fixes the exact version when you submit, so every task runs the
                same software. Private image? Add a read-only token once under <Link to="/images">My images</Link> (or a
                coordinator adds one for the group on the <em>Images</em> tab). A job never contains a password.
              </li>
              <li>
                <strong>Command</strong>: what to run, as in a terminal. Leave it empty to run the image's own command.
              </li>
              <li>
                <strong>How to run</strong>: <em>Once</em>; <em>Split into parts</em> (<em>Auto</em>: as many as the
                group's computers can run at once), where each part gets its number as <code>{"{{index}}"}</code> and in{" "}
                <code>POOL_TASK_INDEX</code>; or <em>For each value</em>, e.g. <code>seed</code> = <code>0..9</code>,
                used as <code>{"{{seed}}"}</code>.
              </li>
              <li>
                <strong>Size</strong>: leave it to pool (the default), and it chooses CPU, memory and disk from your
                earlier runs, and says why on the job page. Or choose a size yourself.
              </li>
              <li>
                Write result files to <code>/out</code>, and one JSON object to <code>/out/result.json</code> to get a
                results table; anything printed is kept in the task's log.
              </li>
            </ul>
          </li>
          <li>
            Open the job to follow it. If tasks wait, a box says <strong>why</strong> and what would fix it. Under{" "}
            <em>Results</em>: <em>Download all (zip)</em> gives every task's files and log, a table of all{" "}
            <code>result.json</code> files and merged CSV files; <em>Download results table (CSV)</em> gives the table
            alone. On the command line: <code>pool download JOB-ID --all</code> and <code>pool results JOB-ID</code>.
          </li>
          <li>
            <em>Run again with changes</em> on a job page opens the form filled with that job. You can cancel your own job
            at any time, and delete it (with its logs and files) with <em>Delete job</em>.
          </li>
        </ol>
        <p>
          <strong>From your terminal:</strong> <code>pool run</code> works like <code>docker run</code>, but on a
          computer of your group.
        </p>
        <ul>
          <li>
            In a folder with a Dockerfile, run <code>pool run -- ARGS</code>. It builds the image on your computer and
            runs it on the group.
          </li>
          <li>
            The output shows in your terminal. The files come back to <code>./pool-out</code>.
          </li>
          <li>
            It takes docker's flags: <code>-e</code>, <code>--cpus</code>, <code>-m</code>, <code>--gpus</code>. It
            works for any language.
          </li>
          <li>
            <code>pool run --local</code> checks it on your computer first, with the same rules.
          </li>
          <li>
            <code>pool attach JOB</code> follows any job, also one you submitted here.
          </li>
        </ul>
        <details>
          <summary>The example job, as a job file</summary>
          <pre>{formToYaml(EXAMPLE_FORM)}</pre>
        </details>
        <p className="muted small-text">
          Jobs have no internet access, cannot read input files from the server, and cannot use secrets yet: put small
          input data into the image. Downloads need the server's file store; without one, output files stay on the
          computer that ran the task. Examples for every kind of work (random runs, sweeps, many files, test suites) are
          in the user guide, docs/user-guide.md §5.
        </p>
      </Section>

      <Section id="share" title="Share a computer (members, contributors, coordinators, admins)">
        <p>
          Your computer runs jobs from your group inside locked-down containers: no access to your files, no network, and
          only the CPU, memory and disk you allow. It needs Docker.
        </p>
        <ol className="steps">
          <li>
            Go to <Link to="/devices">My computers</Link> and follow the four steps there: get the agent, connect it with a
            one-time code, choose a group with <code>pool-agent share GROUP</code>, and start it with{" "}
            <code>pool-agent run</code>.
          </li>
          <li>
            Set limits when you share, for example{" "}
            <code>pool-agent share my-lab --cpu-percent 50 --memory 8Gi --only-when-idle 10m</code>. Run the command again
            to change them.
          </li>
          <li>
            <strong>Stop everything now:</strong> <code>pool-agent kill</code> (and <code>pool-agent resume</code> to allow
            work again). <strong>Stop sharing with one group:</strong> <code>pool-agent unshare GROUP</code>, or{" "}
            <em>Turn off</em> on the computer's page.
          </li>
          <li>
            See what ran on your computer and who sent it: the computer's page, section <em>What ran here</em>, or{" "}
            <code>pool-agent status</code>.
          </li>
        </ol>
      </Section>

      <Section id="coordinator" title="Coordinators">
        <ul>
          <li>
            <strong>Two-step sign-in:</strong> on a shared server, invites, settings, roles and votes need a code from an
            authenticator app on your phone. The banner at the top of the page sets it up once.
          </li>
          <li>
            <strong>Invite people:</strong> tab <em>Invites</em> → choose role, number of uses and expiry → <em>Create
            invite</em> → copy the link and send it by chat or email.
          </li>
          <li>
            <strong>Join requests:</strong> tab <em>Join requests</em>. Who approves depends on the group's rule
            (coordinator only, or a number of members).
          </li>
          <li>
            <strong>Members:</strong> tab <em>Members</em> to change someone's role or remove them. Work running on a
            removed member's computers for this group stops and goes back to the queue.
          </li>
          <li>
            <strong>Settings:</strong> tab <em>Settings</em> for the approval rule and other group options, and to close the
            group when the work is done.
          </li>
          <li>
            <strong>Private images:</strong> tab <em>Images</em> → store one <em>read-only</em> token for the
            group's registry (ghcr.io or Docker Hub). Members push with their own accounts; images in someone's personal
            account use that person's own token (<em>My images</em>). Computers only ever get short-lived access to the
            one image a task needs.
          </li>
          <li>
            <strong>Computers:</strong> tab <em>Devices</em> → <em>Remove</em> takes a computer out of the group; it
            stays out until you choose <em>Let back in</em>. Only its owner can disable a computer entirely.
          </li>
          <li>
            <strong>Deleting:</strong> <em>Delete job</em> on any job page. To delete the whole group: tab{" "}
            <em>Settings</em> → <em>Delete group</em>. It disappears at once; for 7 days you can restore it from the Groups
            page, then its jobs, logs and files are gone. The audit log and each computer owner's "what ran here" list are
            kept.
          </li>
          <li>
            <strong>Fairness and records:</strong> tab <em>Usage</em> shows who used how much; tab <em>Audit log</em>{" "}
            records every important action and cannot be edited.
          </li>
        </ul>
      </Section>

      <Section id="admin" title="Admins">
        <p>
          Admins help the coordinator: they create invite links (for members, contributors and submitters), handle join
          requests, read the audit log, and can cancel any job in the group. They can also run jobs and share computers.
        </p>
      </Section>

      <Section id="server-admin" title="Server admins">
        <ul>
          <li>
            Run the server (see the project README) and keep it updated. Server admins are listed in{" "}
            <code>POOL_SERVER_ADMINS</code>.
          </li>
          <li>
            Mark organisation-owned, dedicated computers: only those run jobs marked <code>confidential</code>. There is no
            screen for this yet; use the API: <code>PATCH /api/v1/devices/ID</code> with{" "}
            <code>{'{"org_owned_dedicated": true}'}</code>.
          </li>
          <li>
            Check that the audit log has not been tampered with: <code>GET /api/v1/audit/verify</code>.
          </li>
          <li>
            Revoke a lost or stolen computer (<em>Revoke…</em> in a group's Devices tab), and restore any deleted group
            during its 7 days (Groups page).
          </li>
        </ul>
      </Section>

      <Section id="help" title="When something is wrong">
        <ul>
          <li>
            <strong>My job is waiting.</strong> Open the job: the <em>why</em> box explains it, for example no computer is
            shared with the group, the computer is offline, or the job asks for more memory than any computer offers.
          </li>
          <li>
            <strong>My computer is not listed in the group.</strong> On that computer, run{" "}
            <code>pool-agent share GROUP</code>, and make sure <code>pool-agent run</code> is running.
          </li>
          <li>
            <strong>A task failed.</strong> Open its log on the job page; lines starting with <code>agent:</code> come from
            the computer that ran it (for example an image that could not be downloaded).
          </li>
          <li>
            <strong>I want it to stop now.</strong> On your computer: <code>pool-agent kill</code>.
          </li>
        </ul>
      </Section>
    </div>
  );
}
