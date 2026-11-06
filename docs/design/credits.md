# Credits: share to earn, use to spend — design proposal

| | |
|---|---|
| **Status** | Proposal. The rules decided on 6 November 2026 are in [§1](#1-goal). The defaults in [§8](#8-defaults-to-confirm) need a yes or a change. Nothing is built. |
| **Date** | 6 November 2026 |
| **Builds on** | Plan §7.7 ("optional credit mode") and §8.2 (threat 12, resource abuse), fair share in the scheduler, the usage ledger, [pool decides](pool-decides.md) |

## 1. Goal

The more you share, the more you can use.

Today, fair share only looks at use: whoever used the least in the last 7
days goes first. Sharing gives you nothing back. So someone who only uses is
treated the same as someone who shares every computer they have. Credits fix
this, without money.

**Decided on 6 November 2026:**

1. You **earn** credits when your computer runs other people's work, and you **spend** them when your work runs on other people's computers.
2. You can **give** credits to another member.
3. You can **use credits in another group** you belong to.
4. Every new member **starts with free credits**.
5. The **coordinator can give free credits** to any member.
6. A member can **ask for free credits**.

## 2. What it is like

**Before and after a run:**

```text
$ pool run
pool: This run costs about 3 credits (4 CPUs, 4 GiB, about 40 min). You have 120 in ml-lab.
...
pool: Done. It cost 2.9 credits. You have 117.1 in ml-lab.
```

**Sharing a computer:**

```text
$ pool-agent status
lab-pc  shared with ml-lab  running 2 tasks  earned this week: 38 credits
```

**Out of credits:**

```text
$ pool run
pool: You have 0 credits in ml-lab. Your job runs only on computers nobody with credits is waiting for.
pool: To get credits: share a computer (pool-agent share ml-lab), ask a member to give you some,
pool: or ask the coordinator: pool credits ask 50 --reason "thesis experiments"
```

**Asking for credits, and the coordinator's answer:**

```text
$ pool credits ask 50 --reason "thesis experiments" --group ml-lab
pool: Asked the coordinator of ml-lab for 50 credits.

$ pool credits requests --group ml-lab            # the coordinator
ID     MEMBER  AMOUNT  REASON              ASKED
r-81   bob     50      thesis experiments  2 h ago
$ pool credits approve r-81 --group ml-lab        # or: approve r-81 30, or: decline r-81
```

## 3. The rules

### 3.1 What one credit is

One credit is one CPU core for one hour. Other resources count as CPU hours:

| For one hour | Credits |
|---|---|
| 1 CPU core | 1 |
| 4 GiB memory | 1 |
| 1 GPU | 10 |
| Disk | free |

The coordinator can change these rates for the group.

**Cost of a task** = (CPU cores + memory in GiB ÷ 4 + GPUs × 10) × hours.

Example: 4 CPUs and 4 GiB for 40 minutes = (4 + 1) × ⅔ = 3.3 credits.

### 3.2 What is charged

| Case | You pay for |
|---|---|
| You set the size | What you reserved. It held that part of the computer. |
| pool set the size ([pool decides](pool-decides.md)) | What the task really used. A first run that holds a whole computer costs only what it used. |
| pool guessed too small and tried again | Nothing for the failed try. |
| The computer went away during the task | Nothing for the lost part. |
| Your program failed | What it used. The computer did the work. |
| Your task ran on your own computer | Nothing. You earn nothing either. |

Long tasks and apps are charged every hour while they run, so balances stay
up to date.

### 3.3 How you earn

- The owner of the computer **earns what the user paid** for the task. Credits move from one member to another. Running work makes no new credits.
- You earn in the group the task belonged to.
- When pool's own guess was wrong ([§3.2](#32-what-is-charged)), the owner still earns for the failed try. pool pays it. These are the only credits pool makes, and they are marked as such.
- A computer that is online but idle earns nothing (default). A coordinator can turn on **"earn while waiting"** at a small rate (e.g. 10%), for groups that want to reward being available.

### 3.4 Free credits

| Kind | Who gets it | How |
|---|---|---|
| **Start credits** | Every new member, once per group | The coordinator sets the amount on the **Settings** tab. Turning credits on gives every member their start credits. |
| **Grant** | Any member the coordinator chooses | The coordinator gives an amount, with a reason. Not to themselves. |
| **Request** | A member who asks | The member asks for an amount, with a reason. The coordinator approves all of it, approves part of it, or declines. One open request per member at a time. |

Free credits are marked "free" in your history. You can spend them and give
them to members of the same group. They cannot move to another group
([§3.6](#36-use-credits-in-another-group)).

### 3.5 Give credits to a member

- `pool credits give bob 10 --group ml-lab --note "thanks for the GPU"`, or **Give credits** in the browser.
- Only to a member of the same group, and only what you have.
- Both of you see it in your history. It is also in the audit log.
- Free credits stay free when you give them.

### 3.6 Use credits in another group

You have one balance in each group. To use credits from one group in another,
you move them:

```text
$ pool credits move 30 --from ml-lab --to vision-lab
pool: Moved 30 credits from ml-lab to vision-lab. You have 87.1 in ml-lab and 30 in vision-lab.
```

| Rule | Why |
|---|---|
| Only between groups you belong to | – |
| Only **earned** credits move, not free ones | A course or a hackathon that gives many free credits must not fill another group with them. |
| Both groups must allow it (Settings: "credits can come in" and "credits can go out", both on by default) | A group can keep its credits to itself. |
| 1 credit = 1 credit | A credit is one CPU hour everywhere. Rates only change how memory and GPUs are counted. |

**Why not one balance for all groups:** groups have different rules: free
credits, rates, who may join. Separate balances, with moves you can see, keep
each group's sharing fair and easy to check.

### 3.7 Out of credits

The coordinator chooses the rule for the group on the **Settings** tab:

| Rule | What happens |
|---|---|
| **Lowest priority** (default) | Your tasks run only on computers nobody with credits is waiting for. Free computers are not wasted. |
| **Wait** | Your tasks wait until you have credits again. |

A running task is never stopped because of credits. Its last hour may take
your balance a little below zero. Your next task then follows the rule.

### 3.8 Credits and fair share

When several people's tasks wait for a computer, the order is:

1. The owner's group priority for that computer (as today).
2. People with credits before people without ([§3.7](#37-out-of-credits)).
3. Among them, fair share: the person who used least in the last 7 days first (as today).

Having more credits does not put you ahead of someone with fewer. Credits
decide whether you can use the group's computers, not who goes first. So
nobody gains by hoarding them.

### 3.9 Turning credits on

| Template | Credits for a new group | Start credits |
|---|---|---|
| Research team | Off (the coordinator can turn them on) | 20 |
| Dev team | On | 20 |
| Student group | On | 20 |
| Course | Off: the teacher shares, the students use, and that is the point | – |
| Hackathon | On | 10 |

Existing groups keep credits off until their coordinator turns them on.

## 4. Where you see it

| Place | What |
|---|---|
| The group's header | Your balance in this group |
| New tab **Credits** | Your balance and history (earned, spent, given, received, free, moved), and the buttons **Give**, **Ask for credits**, **Move** |
| Same tab, for the coordinator | Open requests (approve or decline), **Grant credits**, and the settings |
| The job form and `pool run` | The expected cost before you start, the real cost after |
| The **Usage** tab | Hours given and hours used per member (as today). Coordinators and admins also see each member's balance. |
| A computer's page | The credits it earned, per group |

**On the command line:**

| Task | Command |
|---|---|
| My balance | `pool credits [--group G]` |
| My history | `pool credits history [--group G]` |
| Give to a member | `pool credits give USER N [--note TEXT]` |
| Ask for free credits | `pool credits ask N --reason TEXT` |
| Move to another group | `pool credits move N --from G1 --to G2` |
| Requests (coordinator) | `pool credits requests`, `pool credits approve ID [N]`, `pool credits decline ID` |
| Grant (coordinator) | `pool credits grant USER N --reason TEXT` |

## 5. How it works

- **One ledger.** Every change is one row in a new append-only table: group, member, amount (plus or minus), kind (start, earn, spend, give, receive, grant, request, move in, move out, pool's retry), free or earned, and a link to the task, request or other row. Rows are never changed or deleted, like the audit log. A balance is the sum of a member's rows.
- **Charging** happens where pool already records usage (when a task's try ends, and every hour for long ones). In one database transaction it writes the user's "spend" row and the owner's "earn" row.
- **No double spending.** Giving, moving and approving check the balance and write both rows in one transaction, with the member's balance locked.
- **Estimates** come from the size and from learned run times ([pool decides](pool-decides.md)).
- **The scheduler** gets one more sort key in its job query: people with credits before people without.

## 6. Delivery order

| Step | Scope | Done when (with tests) |
|---|---|---|
| 1. Ledger | The ledger, charging and earning, balances, start credits, on and off per group | A task on another member's computer moves the right credits to its owner. One on your own computer moves none. |
| 2. Out of credits | The scheduler rule and its two settings | With "lowest priority", a member with 0 credits runs only when nobody with credits waits. With "wait", not at all. |
| 3. Free credits and giving | Grants, requests, giving: API, web, CLI, audit log | A coordinator cannot grant to themselves. A request can be approved in part. Free credits stay free when given. |
| 4. Other groups | Moving credits between groups | Only earned credits move, and only when both groups allow it. |
| 5. Estimates | The cost before and after, in the form and in `pool run` | The estimate is shown before submitting. The real cost is shown at the end. |

Credits can ship before [pool decides](pool-decides.md). Until pool measures
use, every task is charged on what it reserved.

## 7. Needs a human review (fairness and security)

| Risk | Answer |
|---|---|
| Fake accounts to collect start credits | Groups are invite-only. Start credits are small, and the coordinator sets them. |
| A coordinator gives themselves credits | Not allowed. Grants to others are in the audit log. |
| Two members run each other's work to collect credits | They only pass credits back and forth: earning always needs someone who pays. |
| An owner's agent reports more use than real, to earn more | Charges never exceed what the task reserved. Use that is far above what the same person's task used on other computers is flagged. |
| The same credits spent twice at the same moment | One transaction with the balance locked. |
| Free credits flow into another group | Free credits cannot move. |
| Privacy | Balances are visible to the member, the coordinator and admins. Members see hours given and used, as today. |

## 8. Defaults to confirm

These are my proposals. Each can be changed.

1. **Rates:** 1 CPU hour = 1 credit, 4 GiB of memory for an hour = 1, 1 GPU hour = 10.
2. **Start credits:** 20 per new member (20 CPU hours).
3. **Who approves requests and grants:** the coordinator only, as decided. Should admins be allowed too?
4. **Earn while waiting:** off by default, and the coordinator can turn it on. (My reading of "all cases make sense".)
5. **Out of credits:** "lowest priority" by default, with "wait" as the other setting. (Also my reading of "all cases make sense".)
6. **Moving credits between groups:** allowed by default. Each group can turn it off.
