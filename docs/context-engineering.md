# Context engineering

## Objective

The context layer reduces the amount of repository material sent to providers while preserving the files most likely to matter. It combines semantic retrieval, structural relationships, recent changes, and project memory. Selection is evidence for an agent, not an authorization boundary.

## Indexing

Indexable extensions are `.py`, `.md`, `.yaml`, `.yml`, `.toml`, and `.txt`. Discovery honors `.gitignore` and excludes generated or high-noise paths such as `.git`, virtual environments, `node_modules`, Python and pytest caches, vector storage, and `uv.lock`.

Chunking is format-aware:

- Python uses Tree-sitter and extracts top-level functions and classes;
- Markdown uses H1/H2 sections;
- other supported text formats are indexed as complete files.

Each chunk is embedded with `all-MiniLM-L6-v2` into 384 dimensions and stored in the target-local Qdrant collection `chunks` using cosine similarity.

## Project graph

A NetworkX `MultiDiGraph` captures several weak signals:

| Edge | Weight | Meaning |
|---|---:|---|
| Python import | 1.0 | Direct structural dependency |
| Documentation reference | 0.6 | Path or symbol mentioned by documentation |
| Git co-change | 0.4 | Files changed together historically |

Retrieval seeds a personalized PageRank calculation with semantic candidates. The current damping factor is 0.85. Graph expansion is applied only when its score improves the candidate set enough to clear the configured lift threshold.

The graph is serialized as JSON in `.ai-platform/graph.json`, keyed by Git HEAD. JSON is used instead of executable serialization. A graph built from a dirty source tree is not written to a HEAD-only cache.

## Selection pipeline

```text
request
  -> semantic candidates above minimum similarity
  -> graph expansion and relevance lift
  -> changed-file and project-memory signals
  -> rank, deduplicate, and cap files
  -> render bounded prompt context
```

The shipped context policy enables Git diff, graph, vector, and memory sources. Relevant defaults include:

- semantic/graph blend ratio: 0.5;
- minimum similarity: 0.20;
- minimum graph lift: 1.2;
- maximum selected files: 20;
- maximum rendered context: 20,000 characters.

Exact values live in `config/context.yaml`.

If no candidate clears the relevance floor, the platform injects no selected files. The provider may still inspect the authorized worktree.

## Snapshot consistency

Run context is built from the integration worktree, so it describes the committed snapshot the agents actually modify. A dirty user checkout is warned about but its uncommitted diff is not injected into a clean HEAD-based run.

Context indexes are currently stored under the target root even though selection reads from the integration worktree. This is a known purity gap: the target checkout can receive `.ai-platform/` writes. Moving all run-scoped storage outside the target is tracked as future hardening.

## Security and privacy

Repository content, documentation, Git history, and memory are untrusted prompt data. They are wrapped as data and mechanically defanged where parser-like control markers are known, but providers may still be influenced by them. Filesystem contracts, provider restrictions, and validation enforce behavior.

Vector indexes contain derived repository content. Do not share or back them up without applying the same confidentiality policy as the source repository.

## Rebuild and performance

The current index is not fully incremental. Large repositories may pay repeated embedding and graph costs; incremental indexing is tracked by [#39](https://github.com/4nass/ai-platform/issues/39). File traversal and batching improvements are also tracked in the existing performance backlog.
