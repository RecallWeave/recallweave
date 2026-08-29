"use client";

import {
  KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  buildColdTrails,
  exportSavedTrailsMarkdown,
  trailPairKey,
  trailTrustLabel,
  trailTypeLabel,
  validatedMutualNeighbors,
  validatedSharedTags,
  type ColdTrail,
  type ColdTrailsFeedback,
  type ColdTrailsResult,
} from "../cold-trails";
import {
  candidateDismissKeys,
  clearDismissedPairDigests,
  createPersistCoordinator,
  filterDismissedPairsByStoredDigests,
  graphFeedbackFingerprint,
  hashDismissedPairKey,
  loadDismissedPairDigests,
  saveDismissedPairDigests,
} from "../cold-trails-feedback-store";
import { citationPath, type GraphDocument, type GraphEdge } from "../graph-data";

type ColdTrailsTourProps = {
  graph: GraphDocument;
  open: boolean;
  onClose: () => void;
  onShowOnMap: (nodeIds: string[]) => void;
  onCopyPath: (path: string) => void;
  onCopyCitation: (citation: string) => void;
  onStatus: (message: string) => void;
};

function initialFeedback(dismissedPairs: Iterable<string> = []): ColdTrailsFeedback {
  return {
    dismissedPairs: new Set(dismissedPairs),
    shownPairs: new Set(),
    usedDomains: new Set(),
    usedTypes: new Map(),
    usedNodeIds: new Set(),
    usedSurpriseTerms: new Set(),
    domainTouchCounts: new Map(),
  };
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return [
    ...root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ];
}

function edgeForTrail(graph: GraphDocument, trail: ColdTrail): GraphEdge | undefined {
  if (!trail.edgeId) return undefined;
  return graph.edges.find((edge) => edge.id === trail.edgeId);
}

function openSourcePath(graph: GraphDocument, trail: ColdTrail): string {
  if (trail.nodeId) {
    return graph.nodes.find((node) => node.id === trail.nodeId)?.path || "";
  }
  const edge = edgeForTrail(graph, trail);
  const citation =
    edge?.evidence?.source_evidence?.citation ||
    edge?.evidence?.target_evidence?.citation ||
    edge?.evidence?.citation ||
    "";
  return citationPath(citation) || graph.nodes.find((node) => node.id === trail.sourceId)?.path || "";
}

export function ColdTrailsTour({
  graph,
  open,
  onClose,
  onShowOnMap,
  onCopyPath,
  onCopyCitation,
  onStatus,
}: ColdTrailsTourProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const feedbackRef = useRef<ColdTrailsFeedback>(initialFeedback());
  const persistRef = useRef(createPersistCoordinator());
  const [result, setResult] = useState<ColdTrailsResult | null>(null);
  const [trails, setTrails] = useState<ColdTrail[]>([]);
  const [index, setIndex] = useState(0);
  const [saved, setSaved] = useState<ColdTrail[]>([]);
  const [explainOpen, setExplainOpen] = useState(false);
  const [announcement, setAnnouncement] = useState("");

  const nodeById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes],
  );
  const authoredAdjacency = useMemo(() => {
    const adjacency = new Map<string, Set<string>>();
    graph.nodes.forEach((node) => adjacency.set(node.id, new Set()));
    graph.edges
      .filter((item) => item.verified)
      .forEach((item) => {
        adjacency.get(item.source)?.add(item.target);
        adjacency.get(item.target)?.add(item.source);
      });
    return adjacency;
  }, [graph.edges, graph.nodes]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      const fingerprint = await graphFeedbackFingerprint(graph);
      const storedDigests = await loadDismissedPairDigests(fingerprint);
      const dismissed = await filterDismissedPairsByStoredDigests(
        candidateDismissKeys(graph),
        storedDigests,
      );
      if (cancelled) return;
      feedbackRef.current = initialFeedback(dismissed);
      const next = buildColdTrails(graph, feedbackRef.current);
      setResult(next);
      setTrails(next.status === "ok" ? next.trails : []);
      setIndex(0);
      setSaved([]);
      setExplainOpen(false);
      setAnnouncement(
        next.status === "ok"
          ? `Cold Trails loaded ${next.trails.length} stops.`
          : `Cold Trails unavailable: ${next.message}`,
      );
      requestAnimationFrame(() => closeRef.current?.focus());
    })().catch(() => {
      if (cancelled) return;
      feedbackRef.current = initialFeedback();
      const next = buildColdTrails(graph, feedbackRef.current);
      setResult(next);
      setTrails(next.status === "ok" ? next.trails : []);
      setAnnouncement(
        next.status === "ok"
          ? `Cold Trails loaded ${next.trails.length} stops.`
          : `Cold Trails unavailable: ${next.message}`,
      );
    });
    return () => {
      cancelled = true;
    };
  }, [graph, open]);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = focusableElements(dialog);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener("keydown", handleKeyDown);
    return () => dialog.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  if (!open) return null;

  const current = trails[index];
  const edge = current ? edgeForTrail(graph, current) : undefined;


  function goNext() {
    if (!trails.length) return;
    setIndex((value) => Math.min(value + 1, trails.length - 1));
    setExplainOpen(false);
    setAnnouncement(`Trail ${Math.min(index + 2, trails.length)} of ${trails.length}.`);
  }

  function goBack() {
    setIndex((value) => Math.max(value - 1, 0));
    setExplainOpen(false);
    setAnnouncement(`Trail ${Math.max(index, 1)} of ${trails.length}.`);
  }

  function skipTrail() {
    goNext();
  }

  function saveTrail() {
    if (!current) return;
    setSaved((items) => (items.some((item) => trailPairKey(item) === trailPairKey(current)) ? items : [...items, current]));
    onStatus("Trail saved to this session.");
    goNext();
  }

  function dismissTrail() {
    if (!current) return;
    feedbackRef.current.dismissedPairs.add(trailPairKey(current));
    const dismissedSnapshot = [...feedbackRef.current.dismissedPairs];
    void persistRef.current.enqueue(async () => {
      const fingerprint = await graphFeedbackFingerprint(graph);
      const digests = await Promise.all(
        dismissedSnapshot.map((key) => hashDismissedPairKey(key)),
      );
      await saveDismissedPairDigests(fingerprint, digests);
    });
    const replacement = buildColdTrails(graph, feedbackRef.current);
    if (replacement.status === "ok") {
      setTrails(replacement.trails);
      setIndex((value) => Math.min(value, Math.max(replacement.trails.length - 1, 0)));
      setResult(replacement);
    }
    onStatus("Trail dismissed for future ranking.");
  }

  function clearHistory() {
    persistRef.current.bump();
    feedbackRef.current = initialFeedback();
    const next = buildColdTrails(graph, feedbackRef.current);
    setResult(next);
    setTrails(next.status === "ok" ? next.trails : []);
    setIndex(0);
    setExplainOpen(false);
    setAnnouncement(
      next.status === "ok"
        ? `Cold Trails history cleared. Loaded ${next.trails.length} stops.`
        : `Cold Trails history cleared. ${next.message}`,
    );
    void persistRef.current
      .enqueue(async () => {
        const fingerprint = await graphFeedbackFingerprint(graph);
        await clearDismissedPairDigests(fingerprint);
        onStatus("Cold Trails dismiss history cleared.");
      })
      .catch(() => {
        onStatus("Could not clear Cold Trails dismiss history from browser storage.");
      });
  }

  function showAnother() {
    if (!current) return;
    trails.forEach((trail) => feedbackRef.current.shownPairs.add(trailPairKey(trail)));
    const replacement = buildColdTrails(graph, feedbackRef.current);
    if (replacement.status !== "ok" || !replacement.trails.length) {
      onStatus("No alternate trail qualified.");
      return;
    }
    setTrails(replacement.trails);
    setIndex(0);
    setResult(replacement);
    setExplainOpen(false);
    setAnnouncement(`Alternate tour with ${replacement.trails.length} stops loaded.`);
  }

  function openSource() {
    const path = current ? openSourcePath(graph, current) : "";
    if (!path) {
      onStatus("No safe source path is available for this trail.");
      return;
    }
    onCopyPath(path);
  }

  function showOnMap() {
    if (!current) return;
    const ids = current.nodeId ? [current.nodeId] : [current.sourceId, current.targetId];
    onShowOnMap(ids.filter(Boolean));
    onStatus("Map framed on trail endpoints.");
  }

  function endTour() {
    if (saved.length) {
      const markdown = exportSavedTrailsMarkdown(graph, saved);
      const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "cold-trails-session.md";
      anchor.click();
      URL.revokeObjectURL(url);
      onStatus("Saved trails exported as Markdown.");
    }
    onClose();
  }

  function handleDialogKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      endTour();
      return;
    }
    const target = event.target as HTMLElement | null;
    const typingField = target?.closest("input, select, textarea, [contenteditable='true']");
    if (typingField) return;
    const activatesControl =
      event.key === " " || event.key === "Enter"
        ? target?.closest("button, a")
        : null;
    if (activatesControl) return;
    if (!current || result?.status !== "ok") return;
    if (event.key === "ArrowRight" || event.key === " ") {
      event.preventDefault();
      goNext();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      goBack();
    } else if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveTrail();
    } else if (event.key.toLowerCase() === "d") {
      event.preventDefault();
      dismissTrail();
    } else if (event.key.toLowerCase() === "e") {
      event.preventDefault();
      setExplainOpen((value) => !value);
    } else if (event.key.toLowerCase() === "o") {
      event.preventDefault();
      openSource();
    }
  }

  function renderEndpoints(trail: ColdTrail) {
    if (trail.nodeId) {
      const node = nodeById.get(trail.nodeId);
      return (
        <div className="cold-trails-endpoints single">
          <div className="cold-trails-endpoint">
            <span className="cold-trails-endpoint-title">{node?.title || trail.nodeId}</span>
            <span className="cold-trails-endpoint-domain">{node?.domain || "Unclassified"}</span>
          </div>
        </div>
      );
    }
    const source = nodeById.get(trail.sourceId);
    const target = nodeById.get(trail.targetId);
    return (
      <div className="cold-trails-endpoints">
        <div className="cold-trails-endpoint">
          <span className="cold-trails-endpoint-title">{source?.title || trail.sourceId}</span>
          <span className="cold-trails-endpoint-domain">{source?.domain || "Unclassified"}</span>
        </div>
        <div className="cold-trails-endpoint">
          <span className="cold-trails-endpoint-title">{target?.title || trail.targetId}</span>
          <span className="cold-trails-endpoint-domain">{target?.domain || "Unclassified"}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="cold-trails-backdrop">
      <button
        type="button"
        className="cold-trails-backdrop-dismiss"
        aria-label="Close Cold Trails"
        onClick={endTour}
      />
      <div
        ref={dialogRef}
        className="cold-trails-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cold-trails-title"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={handleDialogKeyDown}
      >
        <div className="cold-trails-header">
          <div>
            <div className="detail-kicker">Guided discovery</div>
            <h2 id="cold-trails-title">Cold Trails</h2>
          </div>
          <button ref={closeRef} className="ghost-button" onClick={endTour} aria-label="Close Cold Trails">
            Close
          </button>
        </div>

        <p className="sr-only" aria-live="polite" aria-atomic="true">
          {announcement}
        </p>

        {result?.status === "refused" ? (
          <div className="cold-trails-refusal" role="status">
            <strong>Cold Trails is not starting this tour.</strong>
            <p>{result.message}</p>
          </div>
        ) : current ? (
          <article className={`cold-trails-card ${current.trust === "candidate" ? "candidate" : current.trust === "structural" ? "structural" : "authored"}`}>
            <div className="cold-trails-card-meta">
              <span>Trail {index + 1} of {trails.length}</span>
              <span className="cold-trails-badge">{trailTypeLabel(current.type)}</span>
              <span className={`cold-trails-trust ${current.trust}`}>
                {trailTrustLabel(current.trust)}
              </span>
            </div>
            <h3>{current.headline}</h3>
            {renderEndpoints(current)}
            {current.surpriseTerms.length > 0 && (
              <div className="cold-trails-surprise">
                <span className="section-label">Surprise terms</span>
                <div className="tag-list">
                  {current.surpriseTerms.map((term) => (
                    <span className="tag" key={term}>{term}</span>
                  ))}
                </div>
              </div>
            )}
            {edge && (
              <div className="cold-trails-evidence">
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
              </div>
            )}
            <ul className="cold-trails-facts">
              {current.structuralFacts.map((fact) => (
                <li key={fact}>{fact}</li>
              ))}
            </ul>
            {current.trust === "candidate" && (
              <p className="cold-trails-caveat">
                Candidate only: overlapping signals are not proof of a factual relationship.
              </p>
            )}
            {result?.status === "ok" && result.notice && (
              <p className="cold-trails-notice">{result.notice}</p>
            )}
            {explainOpen && (
              <div className="cold-trails-breakdown">
                <span className="section-label">Score breakdown</span>
                <ul>
                  <li>Novelty: {current.scoreBreakdown.novelty.toFixed(2)}</li>
                  <li>Distance: {current.scoreBreakdown.distance.toFixed(2)}</li>
                  <li>Evidence: {current.scoreBreakdown.evidence.toFixed(2)}</li>
                  <li>Centrality: {current.scoreBreakdown.centrality.toFixed(2)}</li>
                  <li>Structure: {current.scoreBreakdown.structure.toFixed(2)}</li>
                  <li>Age bonus: {current.scoreBreakdown.ageBonus.toFixed(2)}</li>
                  <li>Penalties: {current.scoreBreakdown.penalties.toFixed(2)}</li>
                  <li>Total: {current.scoreBreakdown.total.toFixed(2)}</li>
                </ul>
                {edge?.evidence?.signals && current.sourceId && current.targetId && (
                  <>
                    <span className="section-label">Evidence signals</span>
                    <ul>
                      {(edge.evidence.signals.lexical_terms || []).map((term) => (
                        <li key={`term-${term}`}>Lexical: {term}</li>
                      ))}
                      {validatedSharedTags(
                        nodeById.get(current.sourceId) || {
                          id: current.sourceId,
                          title: "",
                          path: "",
                        },
                        nodeById.get(current.targetId) || {
                          id: current.targetId,
                          title: "",
                          path: "",
                        },
                        edge,
                      ).map((tag) => (
                        <li key={`tag-${tag}`}>Shared tag: {tag}</li>
                      ))}
                      {validatedMutualNeighbors(edge, authoredAdjacency).map((id) => (
                        <li key={`neighbor-${id}`}>Mutual neighbor: {id}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
          </article>
        ) : null}

        <div className="cold-trails-controls" role="group" aria-label="Cold Trails controls">
          <button className="ghost-button" onClick={goBack} disabled={!current || index === 0}>Back</button>
          <button className="ghost-button" onClick={skipTrail} disabled={!current || index >= trails.length - 1}>Skip</button>
          <button className="primary-button" onClick={goNext} disabled={!current || index >= trails.length - 1}>Next</button>
          <button className="ghost-button" onClick={saveTrail} disabled={!current}>Save</button>
          <button className="ghost-button" onClick={dismissTrail} disabled={!current}>Dismiss</button>
          <button
            className="ghost-button"
            onClick={clearHistory}
            aria-label="Clear Cold Trails dismiss history"
          >
            Clear history
          </button>
          <button className="ghost-button" onClick={() => setExplainOpen((value) => !value)} disabled={!current}>
            Explain
          </button>
          <button className="ghost-button" onClick={openSource} disabled={!current}>Open source</button>
          <button className="ghost-button" onClick={showAnother} disabled={!current}>Show me another</button>
          <button className="ghost-button" onClick={showOnMap} disabled={!current}>Show on map</button>
          <button className="ghost-button" onClick={endTour}>End tour</button>
        </div>
        {saved.length > 0 && (
          <p className="cold-trails-saved-count" role="status">{saved.length} trail{saved.length === 1 ? "" : "s"} saved this session.</p>
        )}
      </div>
    </div>
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
  onCopyPath: (path: string) => void;
}) {
  if (!evidence?.citation && !evidence?.passage) return null;
  const path = evidence.citation ? citationPath(evidence.citation) : "";
  return (
    <div className="evidence-side">
      <span className="evidence-side-label">{label}</span>
      {evidence.passage && <p>{evidence.passage}</p>}
      {evidence.citation && (
        <div className="citation-row">
          <code title={evidence.citation}>{evidence.citation}</code>
          <button onClick={() => onCopyCitation(evidence.citation!)}>Copy citation</button>
          {path && <button onClick={() => onCopyPath(path)}>Copy path</button>}
        </div>
      )}
    </div>
  );
}
