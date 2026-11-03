/**
 * How to make a read-only token, per registry. Checked against Docker's and GitHub's documentation on
 * 3 November 2026 (docs/user-guide.md §6, Private images); keep both in step when either site changes.
 */
export function TokenSteps({ registry }: { registry: string }) {
  if (registry === "ghcr.io") {
    return (
      <ol className="small-text">
        <li>
          On github.com: your picture (top right) → <strong>Settings</strong> → <strong>Developer settings</strong> →{" "}
          <strong>Personal access tokens</strong> → <strong>Tokens (classic)</strong> →{" "}
          <strong>Generate new token (classic)</strong>.
        </li>
        <li>
          Tick only <code>read:packages</code> and choose an expiry date. Fine-grained tokens do not work with ghcr.io.
        </li>
        <li>Generate it and copy it right away: GitHub shows it only once.</li>
        <li>The account name is your GitHub user name.</li>
      </ol>
    );
  }
  if (registry === "docker.io") {
    return (
      <ol className="small-text">
        <li>
          At app.docker.com: your avatar (top right) → <strong>Account settings</strong> →{" "}
          <strong>Personal access tokens</strong> → <strong>Generate new token</strong>.
        </li>
        <li>
          Access permissions: <strong>Repo Read-only</strong>. Not the default, <em>Repo Public Read-only</em>, which
          cannot read private images. Choose an expiry date.
        </li>
        <li>Generate it and copy it right away: Docker Hub shows it only once.</li>
        <li>The account name is your Docker ID.</li>
      </ol>
    );
  }
  return (
    <p className="small-text">
      Use a token that can only read (pull) images. The registry must use token authentication, as GHCR and Docker Hub
      do; registries that only accept a password on every request are not supported.
    </p>
  );
}
