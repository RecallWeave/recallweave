"use client";

import {
  ChangeEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  WheelEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  GraphDocument,
  GraphEdge,
  GraphNode,
  MAX_FILE_BYTES,
  VIEWER_SCHEMA_V2,
  citationPath,
  importDiagnosticMessage,
  normalizeGraph,
} from "../graph-data";
import { AtlasExportPrivacyChrome } from "./AtlasExportPrivacyChrome";
import { ColdTrailsTour } from "./ColdTrailsTour";

type PositionedNode = GraphNode & {
  x: number;
  y: number;
  degree: number;
  color: string;
};

const PALETTE = ["#d9ff72", "#70e5d3", "#7ca8ff", "#b79bff", "#ff796c", "#f3efe4"];
const NODE_LIST_LIMIT = 100;
const LEGEND_DOMAIN_LIMIT = PALETTE.length;

function buildLayout(graph: GraphDocument): PositionedNode[] {
  const degree = new Map<string, number>();
  graph.edges.forEach((edge) => {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  });
  const domains = Array.from(
    new Set(graph.nodes.map((node) => node.domain || "Unclassified")),
  ).sort();
  const domainMap = new Map(domains.map((item, index) => [item, index]));
  const groups = new Map<string, GraphNode[]>();
  graph.nodes.forEach((node) => {
    const nodeDomain = node.domain || "Unclassified";
    const group = groups.get(nodeDomain) || [];
    group.push(node);
    groups.set(nodeDomain, group);
  });
  const width = 1080;
  const height = 680;
  const orbit = Math.min(width, height) * 0.28;
  const result: PositionedNode[] = [];
  domains.forEach((item, domainIndex) => {
    const angle = (Math.PI * 2 * domainIndex) / Math.max(1, domains.length) - Math.PI / 2;
    const centerX = width / 2 + Math.cos(angle) * orbit;
    const centerY = height / 2 + Math.sin(angle) * orbit * 0.72;
    const members = (groups.get(item) || []).sort(
      (a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0),
    );
    members.forEach((node, index) => {
      const localAngle = index * 2.399963229728653 + domainIndex * 0.53;
      const localRadius = 22 + Math.sqrt(index) * 38;
      result.push({
        ...node,
        x: centerX + Math.cos(localAngle) * localRadius,
        y: centerY + Math.sin(localAngle) * localRadius,
        degree: degree.get(node.id) || 0,
        color: PALETTE[(domainMap.get(item) || 0) % PALETTE.length],
      });
    });
  });
  return result;
}

function evidenceText(edge: GraphEdge): string {
  if (edge.evidence?.explanation) return edge.evidence.explanation;
  const signals = edge.evidence?.signals;
  if (signals?.lexical_terms?.length) {
    return `Shared language: ${signals.lexical_terms.join(", ")}`;
  }
  if (edge.evidence?.shared_terms?.length) {
    return `Shared language: ${edge.evidence.shared_terms.join(", ")}`;
  }
  if (signals?.shared_tags?.length) {
    return `Shared tags: ${signals.shared_tags.join(", ")}`;
  }
  return edge.verified
    ? "Authored in the source note."
    : "Candidate only: a local similarity signal worth reviewing.";
}

function nodeMatches(node: GraphNode, query: string, domain: string): boolean {
  const needle = query.trim().toLocaleLowerCase();
  const queryMatch =
    !needle ||
    `${node.title} ${node.path} ${node.domain} ${(node.tags || []).join(" ")}`
      .toLocaleLowerCase()
      .includes(needle);
  return queryMatch && (domain === "All domains" || node.domain === domain);
}

export function GraphExplorer() {
  const [graph, setGraph] = useState<GraphDocument | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showVerified, setShowVerified] = useState(true);
  const [showCandidates, setShowCandidates] = useState(true);
  const [domain, setDomain] = useState("All domains");
  const [loadError, setLoadError] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [nodeNavigatorFocusId, setNodeNavigatorFocusId] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0);
  const [coldTrailsOpen, setColdTrailsOpen] = useState(false);
  const coldTrailsTriggerRef = useRef<HTMLButtonElement>(null);
  const [mapFocusIds, setMapFocusIds] = useState<string[]>([]);
  const [mapFocusKey, setMapFocusKey] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const nodeButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const userLoadedRef = useRef(false);
  const sampleAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    sampleAbortRef.current = controller;
    fetch("/sample-graph.json", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Sample unavailable");
        return response.json();
      })
      .then((data) => {
        if (!userLoadedRef.current) setGraph(normalizeGraph(data));
      })
      .catch((error: unknown) => {
        if (
          !userLoadedRef.current &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          setLoadError("The sample graph could not be loaded.");
        }
      });
    return () => controller.abort();
  }, []);

  const positioned = useMemo(() => (graph ? buildLayout(graph) : []), [graph]);
  const domains = useMemo(
    () =>
      Array.from(new Set((graph?.nodes || []).map((node) => node.domain || "Unclassified"))).sort(),
    [graph],
  );
  const matchingNodes = useMemo(
    () => positioned.filter((node) => nodeMatches(node, query, domain)),
    [positioned, query, domain],
  );
  const selected = positioned.find((node) => node.id === selectedId) || null;
  const visibleEdges = useMemo(
    () =>
      (graph?.edges || []).filter(
        (edge) => (edge.verified ? showVerified : showCandidates),
      ),
    [graph, showVerified, showCandidates],
  );
  const selectedConnections = selected
    ? visibleEdges
        .filter((edge) => edge.source === selected.id || edge.target === selected.id)
        .sort((a, b) => Number(b.verified) - Number(a.verified))
    : [];
  const authoredConnections = selectedConnections.filter((edge) => edge.verified);
  const candidateConnections = selectedConnections.filter((edge) => !edge.verified);
  const verifiedCount = graph?.edges.filter((edge) => edge.verified).length || 0;
  const candidateCount = graph?.edges.filter((edge) => !edge.verified).length || 0;
  const diagnosticMessage = graph
    ? importDiagnosticMessage(graph.import_diagnostics)
    : "";
  const nodeBrowserItems = matchingNodes.slice(0, NODE_LIST_LIMIT);
  const rovingNodeId =
    nodeBrowserItems.find((node) => node.id === nodeNavigatorFocusId)?.id
    || nodeBrowserItems.find((node) => node.id === selectedId)?.id
    || nodeBrowserItems[0]?.id
    || null;

  function selectNode(id: string | null, moveFocus = false) {
    setSelectedId(id);
    setCopyStatus("");
    setNodeNavigatorFocusId(null);
    if (id && moveFocus) {
      requestAnimationFrame(() => detailHeadingRef.current?.focus());
    }
  }

  function resetExplorer(moveFocusToSearch = false) {
    setSelectedId(null);
    setQuery("");
    setDomain("All domains");
    setShowVerified(true);
    setShowCandidates(true);
    setLoadError("");
    setCopyStatus("");
    setResetKey((value) => value + 1);
    if (moveFocusToSearch) {
      requestAnimationFrame(() => searchRef.current?.focus());
    }
  }

  async function loadFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      if (file.size > MAX_FILE_BYTES) {
        throw new Error(`That file is larger than the ${MAX_FILE_BYTES / 1024 / 1024} MB viewer limit.`);
      }
      const parsed = JSON.parse(await file.text());
      const next = normalizeGraph(parsed);
      userLoadedRef.current = true;
      sampleAbortRef.current?.abort();
      setGraph(next);
      resetExplorer(false);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "Could not load that graph.");
    } finally {
      event.target.value = "";
    }
  }

  async function copyToClipboard(text: string, successMessage: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      setCopyStatus(successMessage);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      try {
        setCopyStatus(
          document.execCommand("copy")
            ? successMessage
            : "Copy failed. Select the text instead.",
        );
      } finally {
        textarea.remove();
      }
    }
  }

  function copyCitation(citation: string) {
    return copyToClipboard(citation, "Citation copied.");
  }

  function copyPath(citation: string) {
    const path = citationPath(citation);
    if (!path) {
      setCopyStatus("Could not derive a safe path from that citation.");
      return;
    }
    return copyToClipboard(path, "Path copied.");
  }

  function copyPlainPath(path: string) {
    if (!path) {
      setCopyStatus("No path available to copy.");
      return;
    }
    return copyToClipboard(path, "Path copied.");
  }

  function showTrailOnMap(nodeIds: string[]) {
    if (!nodeIds.length) return;
    setSelectedId(nodeIds[0]);
    setMapFocusIds(nodeIds);
    setMapFocusKey((value) => value + 1);
  }

  function moveNodeNavigatorFocus(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentId: string,
  ) {
    const currentIndex = nodeBrowserItems.findIndex((node) => node.id === currentId);
    if (currentIndex < 0) return;
    let nextIndex: number;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % nodeBrowserItems.length;
    } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + nodeBrowserItems.length) % nodeBrowserItems.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = nodeBrowserItems.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const nextId = nodeBrowserItems[nextIndex]?.id;
    if (nextId) {
      setNodeNavigatorFocusId(nextId);
      nodeButtonRefs.current.get(nextId)?.focus();
    }
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#atlas">Skip to graph explorer</a>
      <header role="banner">
      <nav className="topbar" aria-label="Primary navigation">
        <a className="brand" href="#atlas" aria-label="RecallWeave Atlas home">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">RecallWeave</span>
          <span className="brand-product">/ Atlas</span>
        </a>
        <div className="top-actions">
          <span className="privacy-pill">
            <span className="privacy-dot" aria-hidden="true" />
            Local browser session
          </span>
          <button className="ghost-button" onClick={() => resetExplorer()}>
            Reset Atlas
          </button>
          {graph && (
            <button
              ref={coldTrailsTriggerRef}
              className="ghost-button"
              onClick={() => setColdTrailsOpen(true)}
            >
              Cold Trails
            </button>
          )}
          <button className="primary-button" onClick={() => fileRef.current?.click()}>
            Load your graph
          </button>
          <input
            ref={fileRef}
            className="file-input"
            type="file"
            accept=".json,application/json"
            onChange={loadFile}
            aria-label="Load RecallWeave graph JSON"
            tabIndex={-1}
          />
        </div>
      </nav>
      </header>

      <main>
      <section className="hero">
        <div>
          <div className="eyebrow">Local-first knowledge cartography</div>
          <h1>
            See the shape of <span>what you know.</span>
          </h1>
        </div>
        <div className="hero-copy">
          <p>
            RecallWeave turns a folder of notes into an evidence-cited map, so
            forgotten thinking, authored links, and surprising connections become visible.
          </p>
          <div className="hero-note">
            <strong>Private by design</strong>
            <span>
              Load a graph file from your computer. It is processed in this browser
              and is not sent to the Atlas application.
            </span>
          </div>
        </div>
      </section>

      <section className="workspace" id="atlas" aria-labelledby="atlas-heading" tabIndex={-1}>
        <h2 id="atlas-heading" className="sr-only">Knowledge graph explorer</h2>
        <AtlasExportPrivacyChrome graph={graph} />
        {(loadError || diagnosticMessage) && (
          <div className={`import-notice ${loadError ? "error" : ""}`} role={loadError ? "alert" : "status"}>
            {loadError || `Import review: ${diagnosticMessage}`}
          </div>
        )}
        <div className="workspace-bar">
          <label className="search-wrap">
            <span className="sr-only">Search nodes</span>
            <input
              className="search"
              ref={searchRef}
              aria-label="Search nodes"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Find a note, idea, or domain..."
            />
          </label>
          <div className="filter-set" role="group" aria-label="Connection filters">
            <button
              className={`filter-button ${showVerified ? "active" : ""}`}
              onClick={() => setShowVerified((value) => !value)}
              aria-pressed={showVerified}
            >
              <span className="edge-key" aria-hidden="true" />
              Authored
            </button>
            <button
              className={`filter-button ${showCandidates ? "active" : ""}`}
              onClick={() => setShowCandidates((value) => !value)}
              aria-pressed={showCandidates}
            >
              <span className="edge-key candidate" aria-hidden="true" />
              Candidates
            </button>
          </div>
          <select
            className="domain-select"
            value={domain}
            onChange={(event) => setDomain(event.target.value)}
            aria-label="Filter by domain"
          >
            <option>All domains</option>
            {domains.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </div>

        <div className="atlas-layout">
          <div className="canvas-stage">
            {graph && (
              <GraphCanvas
                nodes={positioned}
                edges={visibleEdges}
                query={query}
                domain={domain}
                selectedId={selectedId}
                resetKey={resetKey}
                focusNodeIds={mapFocusIds}
                focusKey={mapFocusKey}
                onSelect={(id) => selectNode(id)}
              />
            )}
            {graph && matchingNodes.length === 0 && (
              <div className="zero-state" role="status">
                <strong>
                  {graph.nodes.length === 0
                    ? "This graph contains no notes."
                    : "No notes match this view."}
                </strong>
                <span>
                  {graph.nodes.length === 0
                    ? "Load another graph to begin exploring."
                    : "Clear the search or reset the filters."}
                </span>
                <button onClick={() => resetExplorer(true)}>Reset Atlas</button>
              </div>
            )}
            <div className="canvas-status">
              <span className="stat-chip">
                <b>{matchingNodes.length}</b> of {graph?.nodes.length || 0} notes
              </span>
              <span className="stat-chip">
                <b>{verifiedCount}</b> authored
              </span>
              <span className="stat-chip">
                <b>{candidateCount}</b> candidates
              </span>
            </div>
            <div className="canvas-help">Drag to pan · scroll to zoom · select a node for evidence</div>
          </div>

          <aside className="detail-panel" aria-label="Evidence drawer">
            <p className="sr-only" aria-live="polite" aria-atomic="true">
              {selected ? `Selected ${selected.title}. Evidence drawer updated.` : ""}
            </p>
            {selected && graph ? (
              <>
                <div className="detail-inner">
                  <div className="detail-kicker">
                    <span>{selected.domain || "Unclassified"}</span>
                    <span className="detail-status">{selected.status || "note"}</span>
                  </div>
                  <h3 ref={detailHeadingRef} tabIndex={-1}>{selected.title}</h3>
                  <div className="node-path" title={selected.path}>{selected.path}</div>
                  {graph.schema_version === VIEWER_SCHEMA_V2 && (
                    <div className="node-provenance-claims" role="note">
                      {selected.content_hash && (
                        <span>Content hash claim: {selected.content_hash.slice(0, 12)}…</span>
                      )}
                      {selected.created_at && (
                        <span>Created claim: {selected.created_at}</span>
                      )}
                      {selected.modified_at && (
                        <span>Modified claim: {selected.modified_at}</span>
                      )}
                    </div>
                  )}
                  <p className="node-summary">
                    {selected.summary || "No summary was included in this graph export."}
                  </p>
                  <div className="tag-list">
                    {(selected.tags || []).map((tag, index) => (
                      <span className="tag" key={`${tag}-${index}`}>
                        #{tag}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="connection-section">
                  <ConnectionGroup
                    title="Authored links"
                    edges={authoredConnections}
                    selected={selected}
                    positioned={positioned}
                    onSelect={(id) => selectNode(id, true)}
                    onCopyCitation={copyCitation}
                    onCopyPath={copyPath}
                  />
                  <ConnectionGroup
                    title="Candidate connections"
                    edges={candidateConnections}
                    selected={selected}
                    positioned={positioned}
                    onSelect={(id) => selectNode(id, true)}
                    onCopyCitation={copyCitation}
                    onCopyPath={copyPath}
                  />
                  {selectedConnections.length === 0 && (
                    <p className="no-connections">No visible connections for this note under the current edge filters.</p>
                  )}
                  {copyStatus && <p className="copy-status" role="status">{copyStatus}</p>}
                </div>
              </>
            ) : (
              <div className="empty-detail">
                <div>
                  <div className="detail-kicker">Evidence drawer</div>
                  <div className="empty-orbit" aria-hidden="true" />
                  <h3>Select a note to inspect the weave.</h3>
                  <p>
                    Authored links record source structure. Candidate connections are
                    explainable discovery prompts, not facts.
                  </p>
                </div>
              </div>
            )}
          </aside>
        </div>

        <section className="node-browser" aria-labelledby="node-browser-heading">
          <div>
            <h3 id="node-browser-heading">Keyboard node navigator</h3>
            <p>Select a note to move focus directly to its evidence drawer.</p>
          </div>
          <div className="node-browser-list">
            {nodeBrowserItems.map((node) => (
              <button
                key={node.id}
                ref={(element) => {
                  if (element) nodeButtonRefs.current.set(node.id, element);
                  else nodeButtonRefs.current.delete(node.id);
                }}
                className={selectedId === node.id ? "active" : ""}
                onClick={() => selectNode(node.id, true)}
                onFocus={() => setNodeNavigatorFocusId(node.id)}
                onKeyDown={(event) => moveNodeNavigatorFocus(event, node.id)}
                tabIndex={node.id === rovingNodeId ? 0 : -1}
                aria-current={selectedId === node.id ? "true" : undefined}
                aria-label={`${node.title}, ${node.domain || "Unclassified"}`}
              >
                <span>{node.title}</span>
                <small>{node.domain || "Unclassified"}</small>
              </button>
            ))}
          </div>
          {matchingNodes.length > NODE_LIST_LIMIT && (
            <p className="node-browser-limit">
              Showing the first {NODE_LIST_LIMIT} matches. Refine the search to reach a specific note.
            </p>
          )}
          {matchingNodes.length === 0 && <p className="node-browser-limit">No matching notes.</p>}
        </section>

        <div className="legend">
          {domains.slice(0, LEGEND_DOMAIN_LIMIT).map((item, index) => (
            <div className="legend-item" key={item}>
              <span className="legend-dot" style={{ background: PALETTE[index] }} />
              <span>{item}</span>
            </div>
          ))}
          {domains.length > LEGEND_DOMAIN_LIMIT && (
            <div className="legend-item legend-overflow">
              +{domains.length - LEGEND_DOMAIN_LIMIT} more domains; colours repeat on the canvas
            </div>
          )}
        </div>
      </section>

      <section className="story-band" aria-labelledby="trust-heading">
        <h2 id="trust-heading" className="sr-only">How RecallWeave keeps the map trustworthy</h2>
        <article className="story-card featured">
          <div className="story-number">01 / EVIDENCE</div>
          <h3>Every line earns its place.</h3>
          <p>
            Citations identify the source note and line range. Copy a citation to
            inspect the supporting passage in your canonical notes.
          </p>
        </article>
        <article className="story-card">
          <div className="story-number">02 / DISCOVERY</div>
          <h3>Surprises, clearly labeled.</h3>
          <p>
            Candidate edges use local structure and language to surface connections
            worth thinking about. They never silently become facts.
          </p>
        </article>
        <article className="story-card">
          <div className="story-number">03 / OWNERSHIP</div>
          <h3>Your notes stay yours.</h3>
          <p>
            The index is disposable. The vault remains canonical. Atlas processes
            your exported graph on-device with no account and no graph upload.
          </p>
        </article>
      </section>
      </main>

      <footer className="footer">
        <span>
          <strong>RecallWeave</strong> — evidence-cited discovery for Obsidian
        </span>
        <span>Open source · local first · candidate connections are never facts</span>
      </footer>
      {graph && (
        <ColdTrailsTour
          graph={graph}
          open={coldTrailsOpen}
          onClose={() => {
            setColdTrailsOpen(false);
            requestAnimationFrame(() => coldTrailsTriggerRef.current?.focus());
          }}
          onShowOnMap={showTrailOnMap}
          onCopyPath={copyPlainPath}
          onCopyCitation={copyCitation}
          onStatus={setCopyStatus}
        />
      )}
    </div>
  );
}

function ConnectionGroup({
  title,
  edges,
  selected,
  positioned,
  onSelect,
  onCopyCitation,
  onCopyPath,
}: {
  title: string;
  edges: GraphEdge[];
  selected: PositionedNode;
  positioned: PositionedNode[];
  onSelect: (id: string) => void;
  onCopyCitation: (citation: string) => void;
  onCopyPath: (citation: string) => void;
}) {
  if (!edges.length) return null;
  return (
    <section className="connection-group">
      <div className="section-label">
        <span>{title}</span>
        <span>{edges.length}</span>
      </div>
      <div className="connection-list">
        {edges.map((edge) => {
          const otherId = edge.source === selected.id ? edge.target : edge.source;
          const other = positioned.find((node) => node.id === otherId);
          return (
            <article className={`connection-card ${edge.verified ? "authored" : "candidate"}`} key={edge.id}>
              <button
                className="connection-target"
                onClick={() => onSelect(otherId)}
                aria-label={`${other?.title || otherId}, ${
                  edge.verified ? "authored connection" : "candidate connection"
                }`}
              >
                <span className="connection-title">
                  <span>{other?.title || otherId}</span>
                  <span className={`connection-kind ${edge.verified ? "" : "candidate"}`}>
                    {edge.verified ? "authored" : "candidate"}
                  </span>
                </span>
                <span className="connection-evidence">{evidenceText(edge)}</span>
              </button>
              <EvidenceSide
                label="Source evidence"
                evidence={edge.evidence?.source_evidence}
                onCopyCitation={onCopyCitation}
                onCopyPath={onCopyPath}
              />
              <EvidenceSide
                label="Target evidence"
                evidence={edge.evidence?.target_evidence}
                onCopyCitation={onCopyCitation}
                onCopyPath={onCopyPath}
              />
            </article>
          );
        })}
      </div>
    </section>
  );
}

function EvidenceSide({
  label,
  evidence,
  onCopyCitation,
  onCopyPath,
}: {
  label: string;
  evidence?: { citation?: string; passage?: string };
  onCopyCitation: (citation: string) => void;
  onCopyPath: (citation: string) => void;
}) {
  if (!evidence?.citation && !evidence?.passage) return null;
  return (
    <div className="evidence-side">
      <span className="evidence-side-label">{label}</span>
      {evidence.passage && <p>{evidence.passage}</p>}
      {evidence.citation && (
        <div className="citation-row">
          <code title={evidence.citation}>{evidence.citation}</code>
          <button onClick={() => onCopyCitation(evidence.citation!)}>Copy citation</button>
          <button onClick={() => onCopyPath(evidence.citation!)}>Copy path</button>
        </div>
      )}
    </div>
  );
}

function GraphCanvas({
  nodes,
  edges,
  query,
  domain,
  selectedId,
  resetKey,
  focusNodeIds = [],
  focusKey = 0,
  onSelect,
}: {
  nodes: PositionedNode[];
  edges: GraphEdge[];
  query: string;
  domain: string;
  selectedId: string | null;
  resetKey: number;
  focusNodeIds?: string[];
  focusKey?: number;
  onSelect: (id: string | null) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const drawRef = useRef<() => void>(() => undefined);
  const transform = useRef({ x: 0, y: 0, scale: 1 });
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const [dragging, setDragging] = useState(false);
  const nodeMap = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const activeIds = useMemo(
    () => new Set(nodes.filter((node) => nodeMatches(node, query, domain)).map((node) => node.id)),
    [nodes, query, domain],
  );

  useEffect(() => {
    transform.current = { x: 0, y: 0, scale: 1 };
    drawRef.current();
  }, [resetKey]);

  useEffect(() => {
    if (!focusNodeIds.length) return;
    const points = focusNodeIds
      .map((id) => nodeMap.get(id))
      .filter((node): node is PositionedNode => Boolean(node));
    if (!points.length) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const minX = Math.min(...points.map((node) => node.x));
    const maxX = Math.max(...points.map((node) => node.x));
    const minY = Math.min(...points.map((node) => node.y));
    const maxY = Math.max(...points.map((node) => node.y));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const span = Math.max(maxX - minX, maxY - minY, 140);
    const baseScale = Math.min(rect.width / 1080, rect.height / 680) * 0.92;
    const fitScale = Math.min(2.2, (Math.min(rect.width, rect.height) * 0.5) / (span * baseScale));
    transform.current = {
      x: -baseScale * fitScale * (cx - 540),
      y: -baseScale * fitScale * (cy - 340),
      scale: fitScale,
    };
    drawRef.current();
  }, [focusKey, focusNodeIds, nodeMap]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, rect.width, rect.height);
      const baseScale = Math.min(rect.width / 1080, rect.height / 680) * 0.92;
      const view = transform.current;
      context.save();
      context.translate(rect.width / 2 + view.x, rect.height / 2 + view.y);
      context.scale(baseScale * view.scale, baseScale * view.scale);
      context.translate(-540, -340);

      edges.forEach((edge) => {
        const source = nodeMap.get(edge.source);
        const target = nodeMap.get(edge.target);
        if (!source || !target) return;
        const relevant = activeIds.has(source.id) && activeIds.has(target.id);
        context.beginPath();
        context.moveTo(source.x, source.y);
        context.lineTo(target.x, target.y);
        context.lineWidth = edge.verified ? 1.15 : 0.8;
        context.strokeStyle = edge.verified
          ? `rgba(243,239,228,${relevant ? 0.26 : 0.045})`
          : `rgba(112,229,211,${relevant ? 0.34 : 0.035})`;
        context.setLineDash(edge.verified ? [] : [4, 6]);
        context.stroke();
      });
      context.setLineDash([]);

      nodes.forEach((node) => {
        const active = activeIds.has(node.id);
        const selected = selectedId === node.id;
        const radius = Math.min(14, 6.5 + Math.sqrt(node.degree) * 1.4);
        context.beginPath();
        context.arc(node.x, node.y, selected ? radius + 7 : radius + 3, 0, Math.PI * 2);
        context.fillStyle = selected
          ? "rgba(217,255,114,0.18)"
          : `rgba(255,255,255,${active ? 0.035 : 0.008})`;
        context.fill();
        context.beginPath();
        context.arc(node.x, node.y, radius, 0, Math.PI * 2);
        context.fillStyle = active ? node.color : "rgba(120,130,126,0.24)";
        context.shadowColor = active ? node.color : "transparent";
        context.shadowBlur = selected ? 18 : active ? 6 : 0;
        context.fill();
        context.shadowBlur = 0;
        if (selected) {
          context.beginPath();
          context.arc(node.x, node.y, radius + 4, 0, Math.PI * 2);
          context.lineWidth = 1;
          context.strokeStyle = "#f3efe4";
          context.stroke();
        }
        if (selected || (active && (view.scale > 1.08 || node.degree >= 4))) {
          context.font = `${selected ? 600 : 500} 11px Geist, Arial`;
          context.fillStyle = selected ? "#f3efe4" : "rgba(225,229,222,0.72)";
          context.fillText(node.title.slice(0, 38), node.x + radius + 7, node.y + 4);
        }
      });
      context.restore();
    };
    drawRef.current = draw;
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.round(rect.width * ratio));
      canvas.height = Math.max(1, Math.round(rect.height * ratio));
      draw();
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    return () => {
      drawRef.current = () => undefined;
      observer.disconnect();
    };
  }, [nodes, edges, selectedId, nodeMap, activeIds]);

  function graphPoint(clientX: number, clientY: number) {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const baseScale = Math.min(rect.width / 1080, rect.height / 680) * 0.92;
    const view = transform.current;
    return {
      x: (clientX - rect.left - rect.width / 2 - view.x) / (baseScale * view.scale) + 540,
      y: (clientY - rect.top - rect.height / 2 - view.y) / (baseScale * view.scale) + 340,
    };
  }

  function selectAt(clientX: number, clientY: number) {
    const point = graphPoint(clientX, clientY);
    const nearest = nodes
      .filter((node) => activeIds.has(node.id))
      .map((node) => ({ node, distance: Math.hypot(node.x - point.x, node.y - point.y) }))
      .sort((a, b) => a.distance - b.distance)[0];
    onSelect(nearest && nearest.distance < 20 ? nearest.node.id : null);
  }

  function onPointerDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, y: event.clientY, moved: false };
    setDragging(true);
  }

  function onPointerMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!drag.current) return;
    const dx = event.clientX - drag.current.x;
    const dy = event.clientY - drag.current.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.current.moved = true;
    transform.current.x += dx;
    transform.current.y += dy;
    drag.current.x = event.clientX;
    drag.current.y = event.clientY;
    drawRef.current();
  }

  function onPointerUp(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (drag.current && !drag.current.moved) selectAt(event.clientX, event.clientY);
    drag.current = null;
    setDragging(false);
  }

  function onWheel(event: WheelEvent<HTMLCanvasElement>) {
    event.preventDefault();
    const factor = event.deltaY > 0 ? 0.9 : 1.1;
    transform.current.scale = Math.max(0.55, Math.min(2.8, transform.current.scale * factor));
    drawRef.current();
  }

  return (
    <canvas
      ref={canvasRef}
      className={`graph-canvas ${dragging ? "dragging" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => {
        drag.current = null;
        setDragging(false);
      }}
      onWheel={onWheel}
      aria-hidden="true"
    />
  );
}
