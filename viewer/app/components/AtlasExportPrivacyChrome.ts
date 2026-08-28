import { createElement, type ReactElement } from "react";

import { type GraphDocument } from "../graph-data.ts";
import { AtlasProvenanceChrome } from "./AtlasProvenanceChrome.ts";

/** Export privacy banner GraphExplorer mounts, including provenance claims. */
export function AtlasExportPrivacyChrome({
  graph,
}: {
  graph: GraphDocument | null;
}): ReactElement {
  const privacyClass = graph?.privacy.includes_passage_text
    ? "contains-excerpts"
    : "structure-only";
  const privacyLabel = graph?.privacy.metadata_conflict
    ? "Export privacy flags conflict with displayed content"
    : graph && graph.nodes.length === 0
      ? "Empty graph"
      : graph?.privacy.includes_passage_text
        ? graph.privacy.declared
          ? "Graph + bounded passage text"
          : "Possible excerpts detected"
        : graph?.privacy.includes_note_derived_terms
          ? "Graph metadata + note-derived terms/text"
          : graph?.privacy.metadata_only
            ? "Graph metadata only"
            : graph?.privacy.declared
              ? "Graph structure · no passages"
              : "Excerpt status not declared";
  const privacyDetail = graph
    ? [
        graph.privacy.includes_paths_titles_tags ? "paths, titles, tags" : "",
        graph.privacy.includes_note_derived_terms ? "note-derived terms/text" : "",
        graph.privacy.includes_passage_text ? "passages" : "no passages",
        graph.privacy.source_claims_generated_locally
          ? "source file claims local generation"
          : "",
      ]
        .filter(Boolean)
        .join(" · ")
    : "Load an export to inspect its privacy profile";
  const privacyConflictDetail = graph?.privacy.metadata_conflict
    ? `Declared profile: ${graph.privacy.declared_export_profile}; inspected content: ${graph.privacy.export_profile}.`
    : "";

  return createElement(
    "div",
    { className: `export-privacy ${privacyClass}`, role: "status" },
    createElement("span", { className: "export-privacy-icon", "aria-hidden": "true" }),
    createElement(
      "span",
      null,
      createElement("strong", null, privacyLabel),
      createElement(
        "span",
        { className: "privacy-detail" },
        privacyDetail,
        graph?.privacy.export_profile && graph.privacy.export_profile !== "undeclared"
          ? ` · profile: ${graph.privacy.export_profile}`
          : "",
      ),
      graph?.privacy.includes_passage_text
        ? " Review before screen sharing or sending this file."
        : null,
      privacyConflictDetail
        ? createElement("span", { className: "privacy-conflict-detail" }, ` ${privacyConflictDetail}`)
        : null,
      graph ? createElement(AtlasProvenanceChrome, { graph }) : null,
    ),
  );
}
