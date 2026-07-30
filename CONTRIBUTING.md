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
