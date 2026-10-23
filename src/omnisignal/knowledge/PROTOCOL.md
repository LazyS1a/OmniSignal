# Knowledge release protocol, schema 1

This is a file-based interchange protocol between independently operated A and B.
The publisher accepts **already reviewed** local material. It does not crawl sites,
invoke a model, approve material, generate real content, or expose an HTTP write API.
Tests use explicitly synthetic images and text; none are seeded into production.

## Storage and trust

```
knowledge/
  .publish.lock
  latest.json
  releases/<release_id>/
    manifest.json
    records.jsonl
    images/<relative-file>
  withdrawals/<sequence>.json
```

`OMNISIGNAL_KNOWLEDGE_DIR` defaults to `/app/data/knowledge`. The server serves only
manifest-listed files, manifests, discovery pointers and signed withdrawal lists.
Private signing keys are separate PEM files supplied to the operator CLI. The CLI
refuses keys located inside the publication root. B's trusted Ed25519 public keys
must be configured out of band, keyed by `key_id`; no downloaded key is trusted.

The publisher never overwrites a release directory or a withdrawal sequence.
Publication happens in a staging directory and renames the completed release into
place before atomic replacement of `latest.json`. Repeating the same release ID
with the same content is idempotent; different content is a conflict. An interrupted
pointer update can be repaired by repeating that exact latest release. Repeating
an old release never moves the pointer backward. Administrators who own the host
can still alter files; application immutability is not a protection against root.

All publication, withdrawal and backup operations share `.publish.lock`. On Linux
the implementation uses nonblocking exclusive `flock`; on Windows it locks byte 0
with `msvcrt`. The lock file persists and must not be replaced or unlinked.

## Manifest and signature

`manifest.json` fields:

```
schema_version: 1
release_id: stable safe identifier
sequence: positive consecutive integer
kind: "full" | "delta"
parent_release_id: null for full; active preceding release ID for delta
created_at: UTC ISO 8601 timestamp
taxonomy_version: nonempty version string
records_count: number of records in this package, NOT total live cases
files: [{path, size, sha256}, ...]
key_id: externally trusted key identifier
signature: base64 Ed25519 signature
```

Signature input is the manifest object **without `signature`**, encoded as UTF-8
JSON using sorted keys, no optional whitespace, unescaped Unicode and no NaN or
Infinity (`json.dumps(..., sort_keys=True, ensure_ascii=False,
separators=(',', ':'), allow_nan=False)`). Signing occurs after `key_id` is added.
The interoperable JSON profile permits only strings, booleans, null, arrays,
objects with string keys, and integers in [-9007199254740991, 9007199254740991].
Floats (including 1.0), NaN, Infinity, unsafe integers and non-string object keys
are rejected recursively, including extension metadata. Decimal metadata must
use a documented decimal-string representation. Keys sort by Unicode code point,
not JavaScript UTF-16 code units; string values retain all whitespace exactly.
`files` includes `records.jsonl` and every referenced image, but not the manifest.
File hashes cover their exact bytes. `latest.json` contains `release_id`, `sequence`
and `manifest_sha256` (SHA-256 of exact manifest file bytes including its newline).
The discovery pointer is not a source of trust: B verifies the signature and saves
its own sequence high-water marks to prevent rollback.

Sequences start at 1 and increase by exactly 1. Full packages contain the complete
current snapshot; deltas contain changed records only and reference the immediately
preceding active release. Every upsert contains a full case and every image needed
for that case. An unchanged case may remain in a previously validated package.

## Records and publication gate

Every claim must explicitly include both `source_ids` and `image_ids` arrays.
Either array may be empty, but their union must contain at least one valid evidence
reference. Missing arrays are rejected rather than defaulted during verification.

Each JSONL line is one operation:

```
{op: "upsert", case_id, revision, content_hash, case}
{op: "delete", case_id, revision, content_hash}
```

For upsert, `content_hash` is SHA-256 of canonical `case`. For delete it is SHA-256
of canonical `{op, case_id, revision}`. IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.
The record's case ID and positive revision must match its nested case. A later
revision must increase or be exactly unchanged, including the SHA-256 of every
referenced image's actual bytes. Changing an image at the same path requires a
higher case revision even when all JSON metadata is identical. The publisher
compares the newly staged file hashes against the prior case's signed manifest;
this does not add fields to the JSONL schema. Historical identities persist
even when a full snapshot omits a case. There is at most one operation per case
in a package.

Required case fields:

```
case_id, revision, title, summary_zh, brand_or_team
review_status: "approved"
display_permission: "allowed"
analysis_version, model_version  # e.g. manual-review-v1 / none for manual analysis
tags: {industry: [nonempty text], medium: [nonempty text]}
sources: [{source_id, url, display_permission: "allowed"}]
images: [{image_id, path, source_id, display_permission: "allowed"}]
claims: [{claim_id, kind, text, source_ids: [...], image_ids: [...]}]
```

At least one source, image and evidence claim is required; every source/image has
an explicitly allowed display permission. An unknown/missing permission or pending
review blocks publication. This is an explicit reviewed assertion, not automated
proof of ownership. `summary_zh` must contain Chinese text. Claims distinguish
`author_statement`, `media_statement`, `visual_observation`, `analysis_inference`.
Every claim needs valid source or image evidence; image source references must be
valid. Stable source/image/claim IDs are unique within their containing case.

Images use the `images/` namespace, have no absolute paths, `..`, `.` or backslashes,
and cannot traverse symlinks. JPEG, PNG and WebP images must pass decoding, with
at most 20 MiB and 40 million pixels per image. A package is limited to 512 MiB,
2,000 records, 1 MiB per record, and 500 source/image/claim entries per case. No
unlisted or unreferenced files are accepted. Source URLs are http(s) URLs without
embedded credentials; they are provenance references and never fetched here.

## Independent withdrawals

Each `withdrawals/<sequence>.json` is signed using the same canonical rules:

```
{schema_version: 1, sequence, created_at, case_ids: [cumulative stable case IDs],
 key_id, signature}
```

Withdrawal sequences are independent, consecutive, and cumulative. A case-level
withdrawal applies across every release and revision. It cannot be canceled by
rolling back content, replaying an old manifest, or publishing a newer case
revision. B must save withdrawn IDs and the highest accepted withdrawal sequence
outside the swappable content version and apply withdrawals to results, direct
image access, citations and already-saved references. The A publisher also rejects
upserts of withdrawn cases. A normal delta delete changes the current snapshot;
a withdrawal is the global revocation mechanism.

## Operator usage

An input configuration contains `release_id`, `sequence`, optional `kind`,
`parent_release_id`, `taxonomy_version`, and `cases` (automatically converted into
upsert records) or explicitly hashed `records` (for mixed upsert/delete packages).

```
python -m omnisignal.knowledge publish --root /app/data/knowledge \
  --input /operator/reviewed.json --asset-root /operator/assets \
  --private-key /operator-secrets/signing.pem --key-id publisher-2026

python -m omnisignal.knowledge withdraw --root /app/data/knowledge \
  --input /operator/withdraw.json \
  --private-key /operator-secrets/signing.pem --key-id publisher-2026
```

Withdrawal input is `{sequence, case_ids}`. Output contains only success, sequence
and release ID; no key material. The read-only distribution process does not need
a private signing key. Only the explicitly authorized publication process needs
access to the signing key; its deployment boundary is configured by the operator.

Python entrypoints: `make_record`, `delete_record`, `publish_release`,
`publish_withdrawals`, `verify_release`, `verify_signature`, `read_latest`.
`verify_release(path, trusted_keys)` returns `(manifest, records)`.
`materialize_release(root, release_id=None)` returns a case-ID map after applying
full/delta state and all withdrawals, useful for publisher-owned local status
counts. It is **not** a replacement for B's independent signature verification and
persisted sequence high-water marks.

## Advertising workbench adapter

`omnisignal.workbench.publishing.publish_assets(root, assets, actor)` is an offline
adapter for reviewed, individual image records. It writes the same schema-1
releases and retains `nova-1` taxonomy compatibility. The first publication is a
full package; subsequent publications are deltas so an unselected, previously
published case is not silently removed. Asset IDs and positive revision numbers
remain the stable case identities. Corrections require a higher asset revision.

The adapter requires `accepted` status, explicit `allowed` display and export
permissions, a traceable HTTP(S) source, and unchanged SHA-256 and dimensions of
the reviewed image. It validates actual decoded bytes and copies them without
changing input files. Unknown permissions block the entire selected batch.
`analysis` permission may remain unknown because this operation invokes no model.

Use `OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE` to provide an existing Ed25519 PEM file
outside the published root. No key is generated automatically. Optional
`OMNISIGNAL_KNOWLEDGE_KEY_ID` supplies the stable public key identifier; otherwise
the adapter derives it from the public-key fingerprint. A consumer still needs
that public key configured in its independent trust store.

The compatible `digital` medium is only a generic digital-image category, not a
claim about the actual placement channel. This fallback is explicitly recorded
in `editorial.legacy_medium_fallback` and `editorial.medium_note`. The two reviewed
purposes, `brand` and `conversion`, are stored in `editorial.marketing_purposes`.
Original `nova-1` vocabulary is not extended. Descriptions, supplied OCR text and
review reasons remain marked operator annotations, not model-verified claims.
The mandatory claim records the image dimensions verified from the exact bytes.

The return value distinguishes local `status: published` from downstream
`receipt_status: unconfirmed`. This adapter has no network delivery, consumer
activation, or synchronization receipt behavior. These require separate consumer
integration and must not be inferred from successful local publication.
