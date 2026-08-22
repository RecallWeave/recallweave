# This branch is an unreviewed checkpoint

`foundry/task-contracts` is a **durability checkpoint**, not approved work. It exists so that
validated local commits are not stranded on one machine. It is pushed
automatically whenever the local suite is green and the tree is clean.

**Do not merge, promote, release, or deploy from this branch.**

- Latest adversarial review verdict: **FAIL** (cycle 25, 2026-08-22)
- Two P0 beads block promotion: `recallweave-nv0` (an operator-written gloss is
  labeled `cited_passage`) and `recallweave-kob` (heading-link re-derivation
  cannot bind the coordinate or heading level). Both are reproduced, filed with
  a diagnosis, and disclosed in the docs where they affect a claim.
- Promotion to `main` happens only through a milestone pull request with human
  merge approval, and only after the adversarial review returns PASS.
- A failing or absent review does **not** block this checkpoint, because the
  checkpoint's only purpose is durability and recovery. It does block every
  promotion, merge, release, deploy, and milestone PR action.

Delete this file as part of the promotion commit. Its presence is the machine
check that the branch has not been approved.
