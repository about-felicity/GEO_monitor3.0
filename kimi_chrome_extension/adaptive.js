(function (root, factory) {
  root.KimiAdaptiveUI = factory();
})(typeof globalThis !== "undefined" ? globalThis : self, function () {
  "use strict";

  // Browser-side relocation inspired by Scrapling's adaptive element matching:
  // remember structural properties, then score current elements by similarity.
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();

  function tokens(value) {
    return Array.from(new Set(clean(value).toLowerCase().split(/[^\p{L}\p{N}_-]+/u).filter((item) => item.length > 1))).slice(0, 40);
  }

  function ratio(left, right) {
    const a = new Set(left || []);
    const b = new Set(right || []);
    if (!a.size && !b.size) return 1;
    if (!a.size || !b.size) return 0;
    let overlap = 0;
    for (const item of a) if (b.has(item)) overlap += 1;
    return overlap / Math.max(a.size, b.size);
  }

  function textRatio(left, right) {
    const a = clean(left).toLowerCase();
    const b = clean(right).toLowerCase();
    if (!a && !b) return 1;
    if (!a || !b) return 0;
    if (a === b) return 1;
    if (a.includes(b) || b.includes(a)) return Math.min(a.length, b.length) / Math.max(a.length, b.length);
    return ratio(tokens(a), tokens(b));
  }

  function stableAttributes(element) {
    const output = {};
    for (const name of ["data-testid", "role", "aria-label", "title", "placeholder", "type", "name", "contenteditable"]) {
      const value = clean(element.getAttribute?.(name));
      if (value) output[name] = value.slice(0, 160);
    }
    return output;
  }

  function region(box, viewport) {
    const width = Math.max(1, viewport?.width || innerWidth || 1);
    const height = Math.max(1, viewport?.height || innerHeight || 1);
    const x = (box.left + box.width / 2) / width;
    const y = (box.top + box.height / 2) / height;
    return `${x < 0.34 ? "left" : x > 0.66 ? "right" : "center"}-${y < 0.34 ? "top" : y > 0.66 ? "bottom" : "middle"}`;
  }

  function pathOf(element) {
    const parts = [];
    let current = element;
    for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
      const role = clean(current.getAttribute?.("role"));
      const testId = clean(current.getAttribute?.("data-testid"));
      parts.unshift(`${current.tagName?.toLowerCase() || "node"}${role ? `[role=${role}]` : ""}${testId ? `[testid=${testId}]` : ""}`);
    }
    return parts;
  }

  function fingerprint(element, role, viewport) {
    if (!(element instanceof Element)) return null;
    const box = element.getBoundingClientRect();
    const parent = element.parentElement;
    const metadataText = clean([
      element.getAttribute("aria-label"), element.getAttribute("title"), element.getAttribute("placeholder"),
      element.getAttribute("data-testid"), role === "new_chat" ? element.innerText || element.textContent : ""
    ].filter(Boolean).join(" ")).slice(0, 200);
    return {
      version: 1,
      role,
      tag: element.tagName.toLowerCase(),
      attributes: stableAttributes(element),
      classes: tokens(String(element.className?.baseVal || element.className || "")),
      text: metadataText,
      parentTag: parent?.tagName?.toLowerCase() || "",
      parentAttributes: parent ? stableAttributes(parent) : {},
      parentClasses: parent ? tokens(String(parent.className?.baseVal || parent.className || "")) : [],
      siblings: parent ? Array.from(parent.children).map((item) => item.tagName.toLowerCase()).slice(0, 30) : [],
      path: pathOf(element),
      region: region(box, viewport),
      size: [Math.round(box.width / 20) * 20, Math.round(box.height / 10) * 10]
    };
  }

  function attributeScore(saved, current) {
    let score = 0;
    let possible = 0;
    const keys = new Set(Object.keys(saved || {}).concat(Object.keys(current || {})));
    for (const key of keys) {
      possible += key === "data-testid" ? 4 : 1;
      if (!saved?.[key] || !current?.[key]) continue;
      if (saved[key] === current[key]) score += key === "data-testid" ? 4 : 1;
      else score += textRatio(saved[key], current[key]) * (key === "data-testid" ? 2 : 0.6);
    }
    return possible ? score / possible : 1;
  }

  function score(saved, element, role, viewport) {
    const current = fingerprint(element, role, viewport);
    if (!saved || !current) return 0;
    let value = 0;
    value += saved.tag === current.tag ? 18 : 0;
    value += attributeScore(saved.attributes, current.attributes) * 55;
    value += ratio(saved.classes, current.classes) * 24;
    value += textRatio(saved.text, current.text) * 28;
    value += saved.parentTag === current.parentTag ? 10 : 0;
    value += attributeScore(saved.parentAttributes, current.parentAttributes) * 18;
    value += ratio(saved.parentClasses, current.parentClasses) * 12;
    value += ratio(saved.siblings, current.siblings) * 10;
    value += ratio(saved.path, current.path) * 18;
    value += saved.region === current.region ? 12 : 0;
    const widthDelta = Math.abs((saved.size?.[0] || 0) - (current.size?.[0] || 0));
    const heightDelta = Math.abs((saved.size?.[1] || 0) - (current.size?.[1] || 0));
    value += Math.max(0, 10 - widthDelta / 60 - heightDelta / 30);
    return Math.round(value * 10) / 10;
  }

  function relocate(saved, candidates, role, options) {
    const threshold = Math.max(20, Number(options?.threshold) || 72);
    const viewport = options?.viewport || { width: innerWidth, height: innerHeight };
    let best = null;
    for (const element of candidates || []) {
      if (!(element instanceof Element)) continue;
      const value = score(saved, element, role, viewport);
      if (!best || value > best.score) best = { element, score: value };
    }
    return best && best.score >= threshold ? best : null;
  }

  return { fingerprint, score, relocate };
});
