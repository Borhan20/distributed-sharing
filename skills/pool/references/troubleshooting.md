# When something goes wrong

Find the message, then do what the last column says. Lines in a task's log
that start with `agent:` come from the computer that ran it. Lines that start
with `pool:` come from the pool command, and are often a hint.

## Contents

1. Before the job runs (`pool run`, `pool submit`)
2. The job waits and does not start
3. A task failed
4. Signing in

## 1. Before the job runs

| Message | Do this |
|---|---|
| `this server has no built-in registry` | Push the image to ghcr.io or Docker Hub yourself, then `pool run IMAGE ...`. |
| `pushing the image ... unauthorized` | The sign-in expired. Ask the user to run `pool login` again. |
| `pushing the image ... x509: certificate signed by unknown authority` | Docker on this computer must trust the server's certificate. Ask the user, who asks the server admin. |
| `your sign-in ends in N minute(s), before the image is uploaded` | Ask the user to run `pool login` again, then retry. |
| `pool run does not take -v ...` (or another flag) | The message says why. Usually: copy the files into the image with `COPY`. |
| `image: ... was not found, or it is private` | Check the name and tag. For a private image, the user adds a read-only token: `pool registry set REGISTRY --username NAME --mine`. |
| `image: ... refused the stored credentials` | The token is wrong or expired. The user stores a new one. |
| `image: cannot reach the registry` | Try again later, or ask the server admin. |
| `unknown field(s) ...` | A typo in the job file. The message lists the allowed fields. |
| `unknown placeholders: {{name}}` | Add `name` to `sweep`, or use `{{index}}`. |

## 2. The job waits and does not start

Run `pool status JOB-ID`. The job page in the browser has a box "tasks
waiting: why" with the exact reason.

| Reason | Do this |
|---|---|
| No computer is shared with this group yet | Someone must share a computer. Tell the user. |
| Shared, but none is online | The owner starts `pool-agent run`. Tell the user. |
| Online but not available right now | Outside the owner's hours, or the owner is using it. Wait. |
| Asks for more than any shared computer offers | Ask for less (`-m`, `--cpus`, `--gpus`, `--disk`), or the user asks an owner to raise their limit. |
| No available computer can run this job | Something no computer offers: GPU, operating system, isolation level. Remove what is not needed. |
| Waiting to retry after a failure | Read the failed task's log (`pool logs JOB-ID --task N`). |

## 3. A task failed

Read the log: `pool logs JOB-ID --task N`.

| The log shows | Cause | Do this |
|---|---|---|
| `Read-only file system`, `EROFS` | It writes outside `/out` and `/tmp` | Write results to `/out`, scratch files to `/tmp`. Point caches at `/tmp`. |
| `Network is unreachable`, `could not resolve host`, `getaddrinfo` | It downloads something while it runs | Download it in the `Dockerfile` instead. |
| `Permission denied`, `EACCES` | It runs as user 65534, not root | Write only to `/out` or `/tmp`. In the Dockerfile: `RUN chmod -R a+rX /app`. |
| `out of memory: the program needed more than its ... MiB`, exit code 137, "stopped with signal 9" | Out of memory | Run it again with twice the memory (`-m`). `pool status JOB-ID` shows in its USED column how much each task reached. |
| `max_runtime_exceeded` | It ran out of time | Raise `--time-limit`. |
| `exec format error` | Built for another kind of computer (pool could not read the image to match it) | Build with `--platform` for the group's computers; `pool run` picks the most common kind by itself. |
| Waiting: "Its image is built for amd64, and no shared computer is" | The group's computers are another kind | Build with `--platform linux/arm64` (or the kind the message names). |
| `agent: ... pull access denied`, `manifest unknown` | The computer could not download the image | Check the name. For a private image, the user adds a read-only token. |
| `the server gave no pull token` | The token is missing, expired, or cannot read this image | The user replaces the token. |
| `this image is private: this computer pulled it for another group` | The image belongs to another group's registry | Use an image this group can read. |
| `agent: outputs kept on the device only` | The server has no working file store | The log still has the output. Tell the user; the server admin must fix it. |
| Another exit code and the program's own error | The program failed | Fix the program. Reproduce it with `pool run --local`. |

A failure that does not happen with `pool run --local` but does on the group
usually means the size: the group's computer gave less memory or time than
this one. Check exit code 137 and `max_runtime_exceeded` first.

## 4. Signing in

| Message | Do this |
|---|---|
| `not logged in or token expired: run pool login` | The user runs `pool login --server URL --token TOKEN` (or `--dev-user NAME` on a local test server). Do not ask for the token in the chat. |
| `cannot reach server: ...` | Check the network and the server address. Ask the user. |
| `403: ...` | The user's role in the group does not allow it. For example, a contributor cannot submit jobs. The text after `403:` says what. |
