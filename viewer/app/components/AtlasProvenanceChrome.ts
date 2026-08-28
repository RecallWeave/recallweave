import { createElement, type ReactElement } from "react";

import { type GraphDocument, formatAtlasProvenanceClaims } from "../graph-data.ts";

/** Visible index-claims chrome for viewer.v2 provenance. */
export function AtlasProvenanceChrome({
  graph,
}: {
  graph: GraphDocument;
}): ReactElement | null {
  const claims = formatAtlasProvenanceClaims(graph);
  if (!claims) return null;
  return createElement(
    "span",
    { className: "privacy-provenance-detail" },
    ` Index claims: ${claims}.`,
  );
}
