# This branch is an unreviewed checkpoint

`foundry/task-contracts` is a **durability checkpoint**, not approved work. It exists so that
validated local commits are not stranded on one machine. It is pushed
automatically whenever the local suite is green and the tree is clean.

**Do not merge, promote, release, or deploy from this branch.**

- Latest adversarial review verdict: **PASS** (cycle 30, 2026-08-23) — no
  findings at any severity, and the reviewer states the tree is safe to merge.
- No open bead blocks promotion. The review gate is therefore **satisfied**.
- What is still missing is **human merge approval**, which is the only reason
  this file is still here. A PASS is not an approval: the milestone PR and the
  merge to `main` are Josh's decisions, not a session's.
- Promotion to `main` happens only through a milestone pull request with human
  merge approval, and only after the adversarial review returns PASS.
- A failing or absent review does **not** block this checkpoint, because the
  checkpoint's only purpose is durability and recovery. It does block every
  promotion, merge, release, deploy, and milestone PR action.

Delete this file as part of the promotion commit. Its presence is the machine
check that the branch has not been approved.
