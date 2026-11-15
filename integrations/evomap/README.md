# Pronoia ↔ EvoMap runtime

This adapter invokes only the public root exports of the official
`@evomap/evolver@2.0.51` and `@evomap/evolver-core@2.0.51` packages. It uses the
official asset provider, Ingestor, selectors, `CycleEngine`, `runEvolutionCycle`,
personality store, experience library, memory graph, wire identity and paired
benchmark. Package versions and integrity digests are pinned in `package-lock.json`.

Install with `npm ci --ignore-scripts`. The adapter is a JSON/stdio process, separate
from the Python backend and frontend bundle. It does not invoke the Evolver CLI,
install editor hooks, execute Gene validation shell strings, publish to the Hub,
claim network bounties, or open upstream issues. It does not require a Hub identity
or join Worker Pool / Evolution Circle. Each experiment owns its asset and memory
directories; frozen task identities and measured outcomes come from Pronoia.

The POSIX EventStore in v2 requires a separately provisioned audit-service UID.
For this explicitly local, single-host mode, the official Ingestor is connected to
an append-only, fsynced local audit store and the Python bridge serializes updates
per experiment. This does not claim the multi-principal guarantees of a Hub setup.

No upstream implementation source is copied into Pronoia. Upstream:
https://github.com/EvoMap/evolver.
