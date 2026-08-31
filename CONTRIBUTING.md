# Contributing

Contributions are welcome, especially small changes that improve evidence
quality, privacy, portability, or test coverage.

Before opening a pull request:

1. run `python -m unittest discover -s tests -v`;
2. use only synthetic notes in tests and examples;
3. confirm no vault paths, database files, credentials, personal names, or
   private note contents are staged;
4. preserve the separation between authored, candidate, and supporting signals;
5. preserve zero note writes and zero network calls in the default core.

Large dependencies and model SDKs should be proposed as optional extras with a
clear offline fallback.

## Scope: local and single-user

RecallWeave OSS is local and single-user by construction. Features that require
hosted execution, cross-machine orchestration, multi-user/RBAC, centralized
approvals, managed secrets/connectors, fleet management, billing/metering, or a
proprietary control plane are out of scope for this repository — they belong in
the separate commercial control plane. See the "Product boundary" section of
[ARCHITECTURE.md](ARCHITECTURE.md).
