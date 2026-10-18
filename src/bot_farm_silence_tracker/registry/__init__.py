"""The shared, crowd-sourced scan registry.

The registry is a directory of JSON-lines files, ``<platform>/<shard>.jsonl``, with
one record per scanned account (see :mod:`bot_farm_silence_tracker.registry.records`).
Account IDs and usernames are base64-encoded (URL-safe, no padding). That hides them
from casual search only: anyone can decode them. The registry is meant for a trusted
research group, not for publication, and it gives the accounts no anonymity.

Modules:

* :mod:`~bot_farm_silence_tracker.registry.records`: the record format, its JSON schema,
  validation, sharding and the canonical file layout.
* :mod:`~bot_farm_silence_tracker.registry.build`: turning a stored run into records,
  merging them into a registry, and the scan-skip lookup.
* :mod:`~bot_farm_silence_tracker.registry.evidence`: evidence drafts for review.
* :mod:`~bot_farm_silence_tracker.registry.verify`: the pull-request spot-check against the
  live sources.
* :mod:`~bot_farm_silence_tracker.registry.reports`: local report drafts for humans to
  submit to platform moderators. Nothing is ever submitted automatically.
"""
