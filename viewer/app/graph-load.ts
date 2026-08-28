import { MAX_FILE_BYTES, normalizeGraph, type GraphDocument } from "./graph-data.ts";

/** Parse a user-loaded Atlas JSON file the same way GraphExplorer.loadFile does. */
export function graphFromLoadedFileText(text: string): GraphDocument {
  return normalizeGraph(JSON.parse(text));
}

/** Reject oversized uploads before reading/parsing file contents. */
export function assertGraphFileWithinLimit(byteLength: number): void {
  if (byteLength > MAX_FILE_BYTES) {
    throw new Error(
      `That file is larger than the ${MAX_FILE_BYTES / 1024 / 1024} MB viewer limit.`,
    );
  }
}
