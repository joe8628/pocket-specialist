"""Knowledge graph: entity extraction, co-occurrence graph, query expansion."""
import sys
import json
from pathlib import Path
from itertools import combinations
import networkx as nx
import spacy

REPO_ROOT = Path(__file__).parent.parent
GRAPH_DIR = REPO_ROOT / "knowledge_graph"
GRAPH_PATH = GRAPH_DIR / "graph.json"

SPACY_MODEL = "en_core_web_sm"

try:
    _nlp = spacy.load(SPACY_MODEL)
except OSError:
    print(f"spaCy model '{SPACY_MODEL}' not found.")
    print(f"Download it with: python -m spacy download {SPACY_MODEL}")
    sys.exit(1)


# ── persistence ───────────────────────────────────────────────────────────────

def _load() -> nx.Graph:
    if GRAPH_PATH.exists():
        return nx.node_link_graph(json.loads(GRAPH_PATH.read_text()))
    return nx.Graph()


def _save(G: nx.Graph) -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    GRAPH_PATH.write_text(json.dumps(nx.node_link_data(G), indent=2))


# ── entity extraction ─────────────────────────────────────────────────────────

def extract_entities(text: str) -> list[str]:
    """Extract meaningful noun phrases from text using spaCy."""
    doc = _nlp(text)
    seen = set()
    entities = []
    for chunk in doc.noun_chunks:
        entity = chunk.text.lower().strip()
        # keep multi-word or meaningful phrases; drop trivial/stop-word-only chunks
        if (
            len(entity) > 3
            and entity not in seen
            and not all(t.is_stop or t.is_punct or t.is_space for t in chunk)
        ):
            seen.add(entity)
            entities.append(entity)
    return entities


# ── graph construction ────────────────────────────────────────────────────────

def update(chunks: list[str], ids: list[str]) -> None:
    """Add or refresh chunk entities and their co-occurrence edges in the graph."""
    G = _load()

    for chunk_text, chunk_id in zip(chunks, ids):
        entities = extract_entities(chunk_text)

        for entity in entities:
            if entity not in G:
                G.add_node(entity, chunks=[])
            node_chunks: list = G.nodes[entity]["chunks"]
            if chunk_id not in node_chunks:
                node_chunks.append(chunk_id)

        for a, b in combinations(entities, 2):
            if G.has_edge(a, b):
                G[a][b]["weight"] += 1
            else:
                G.add_edge(a, b, weight=1)

    _save(G)


# ── query expansion ───────────────────────────────────────────────────────────

def expand(query: str, hops: int = 2, top_n: int = 10) -> list[str]:
    """Return related concepts for a query by traversing the knowledge graph."""
    G = _load()
    if G.number_of_nodes() == 0:
        return []

    query_entities = set(extract_entities(query))
    scores: dict[str, float] = {}

    for entity in query_entities:
        if entity not in G:
            continue

        first_hop = sorted(
            G[entity].items(), key=lambda x: x[1].get("weight", 0), reverse=True
        )
        for neighbour, data in first_hop[:top_n]:
            w = data.get("weight", 0)
            scores[neighbour] = scores.get(neighbour, 0) + w

            if hops >= 2:
                second_hop = sorted(
                    G[neighbour].items(),
                    key=lambda x: x[1].get("weight", 0),
                    reverse=True,
                )[:3]
                for second, data2 in second_hop:
                    if second not in query_entities:
                        scores[second] = scores.get(second, 0) + data2.get("weight", 0) * 0.5

    # exclude terms already in the query
    return [
        c for c in sorted(scores, key=scores.__getitem__, reverse=True)
        if c not in query_entities
    ][:top_n]


# ── concept map ───────────────────────────────────────────────────────────────

def concept_map(query: str, hops: int = 2) -> dict[str, list[str]]:
    """Return a neighbourhood map of concepts related to the query."""
    G = _load()
    result: dict[str, list[str]] = {}

    for entity in extract_entities(query):
        if entity not in G:
            continue
        first = sorted(
            G[entity].items(), key=lambda x: x[1].get("weight", 0), reverse=True
        )[:8]
        result[entity] = [n for n, _ in first]

        if hops >= 2:
            for neighbour, _ in first[:3]:
                if neighbour not in result:
                    second = sorted(
                        G[neighbour].items(),
                        key=lambda x: x[1].get("weight", 0),
                        reverse=True,
                    )[:5]
                    result[neighbour] = [n for n, _ in second]

    return result


# ── stats ─────────────────────────────────────────────────────────────────────

def stats() -> dict[str, int]:
    G = _load()
    return {"nodes": G.number_of_nodes(), "edges": G.number_of_edges()}
