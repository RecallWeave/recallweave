export function inlineScriptBodies(html: string): string[] {
  const bodies: string[] = [];
  const lowerHtml = html.toLowerCase();
  let cursor = 0;

  while (cursor < html.length) {
    const scriptStart = lowerHtml.indexOf("<script", cursor);
    if (scriptStart < 0) break;

    const boundary = html[scriptStart + "<script".length];
    if (boundary && !/[\s/>]/u.test(boundary)) {
      cursor = scriptStart + "<script".length;
      continue;
    }

    let quote = "";
    let openEnd = -1;
    for (let index = scriptStart + "<script".length; index < html.length; index += 1) {
      const character = html[index];
      if (quote) {
        if (character === quote) quote = "";
      } else if (character === '"' || character === "'") {
        quote = character;
      } else if (character === ">") {
        openEnd = index;
        break;
      }
    }
    if (openEnd < 0) break;

    const attributes = html.slice(scriptStart + "<script".length, openEnd);
    const closePattern = /<\/script\s*>/giu;
    closePattern.lastIndex = openEnd + 1;
    const close = closePattern.exec(html);
    if (!close) break;

    if (!/(?:^|[\s/])src(?:\s|=|\/|$)/iu.test(attributes)) {
      bodies.push(html.slice(openEnd + 1, close.index));
    }
    cursor = close.index + close[0].length;
  }

  return bodies;
}
