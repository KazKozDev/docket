# API stability

Docket follows Semantic Versioning for interfaces used by applications that
embed the library.

## Public interfaces

The supported public surface is:

- every Python name listed in `docket.__all__`;
- the `docket` and `docket-api` commands, including their subcommands, flags,
  documented output and exit codes;
- the HTTP API described by `docs/openapi.json`;
- documented `DOCKET_*` environment variables;
- the serialized `DocumentResult` format described below.

Importable submodules and names outside `docket.__all__` are implementation
details unless another document explicitly declares them public. They may
change in a minor release. Plugins should use the public registration hooks
exported from `docket`, rather than importing registry internals.

The snapshot in `tests/snapshots/public_api.json` records the Python names,
key function signatures, and fields of the options and result models. An
intentional public API change must update that snapshot and the changelog.

## Semantic Versioning

A major release may remove or rename public names, commands, flags, result
fields, environment variables, or HTTP operations. It may also make accepted
inputs invalid, change a field's meaning, or add a new required argument or
result field.

A minor release may add public names, optional parameters, commands, flags,
HTTP operations, enum values, document schemas, and optional result fields.
Callers should ignore unknown JSON fields and should not assume that an enum
or registry is exhaustive unless its documentation says so. Existing calls
and previously valid inputs must continue to work.

A patch release fixes behavior without intentionally changing the public
contract.

## Deprecation

Public API is deprecated before it is removed. A deprecated Python entry point
emits `DeprecationWarning` through `docket._deprecation.warn_deprecated`, its
replacement and planned removal are documented, and the change is recorded in
`CHANGELOG.md`. Removal happens no earlier than the next minor release after
the deprecation shipped, and only in a major release unless retaining the API
would create a security or legal problem.

CLI and HTTP deprecations use their native warning or response mechanism and
follow the same minimum notice period. Security fixes may require a shorter
window; such exceptions are called out prominently in the changelog.

## Result JSON and schema versions

`DocumentResult.model_dump(mode="json")` is the stable serialized result. In a
minor release Docket may add optional fields, add enum values, or populate a
previously optional field. It does not remove fields, rename them, change their
type or meaning, or make an optional field required. Consumers should preserve
or ignore fields they do not understand.

`schema_id` identifies the document schema and `schema_version` identifies the
version used for `extracted`. A schema version is part of persisted data and
must be stored with that data. Compatible additions increment the schema's
minor version; removals, renames, type changes, and changed field semantics
require a new major schema version. Registered migrations are used when a
stored result is read against a newer schema. Applications must not infer a
schema version from the installed Docket package version.
