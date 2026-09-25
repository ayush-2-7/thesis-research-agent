/* Minimal, safe Markdown -> HTML for agent answers.
 *
 * Agent output is untrusted (it can quote fetched web pages or paper
 * abstracts), so every character of input is HTML-escaped FIRST and only
 * then turned into a fixed set of tags. Links are only emitted for http(s)
 * URLs. No raw HTML passes through.
 *
 * Supports: headings, paragraphs, bold/italic/strike, inline code, fenced
 * code blocks, blockquotes, nested lists (incl. task lists), GFM tables,
 * horizontal rules, links and bare URLs.
 */
(function () {
  "use strict";

  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
     .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const LINK_ATTRS = 'target="_blank" rel="noopener noreferrer"';

  // Inline formatting on ONE already-escaped string.
  function inline(s) {
    const slots = [];
    const hold = (html) => `\u0000${slots.push(html) - 1}\u0000`;

    s = s.replace(/`([^`]+)`/g, (_, c) => hold(`<code>${c}</code>`));
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, t, u) => hold(`<a href="${u}" ${LINK_ATTRS}>${t}</a>`));
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?'"])/g,
      (_, p, u) => p + hold(`<a href="${u}" ${LINK_ATTRS}>${u}</a>`));
    s = s.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
         .replace(/__([^_]+?)__/g, "<strong>$1</strong>")
         .replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\w)/g, "$1<em>$2</em>")
         .replace(/(^|[^_\w])_(?!\s)([^_]+?)_(?!\w)/g, "$1<em>$2</em>")
         .replace(/~~([^~]+?)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[+i]);
  }

  const reFence = /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/;
  const reHeading = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  const reHr = /^\s*([-*_])(\s*\1){2,}\s*$/;
  const reQuote = /^\s*>/;
  const reItem = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const reTableSep = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  const indentOf = (l) => l.match(/^\s*/)[0].replace(/\t/g, "    ").length;
  const isItem = (l) => reItem.test(l);
  const isBlockStart = (l, next) =>
    reFence.test(l) || reHeading.test(l) || reHr.test(l) || reQuote.test(l) ||
    isItem(l) || (l.includes("|") && next !== undefined && reTableSep.test(next));

  function codeBlock(code, lang) {
    return `<div class="code"><div class="code-head"><span>${esc(lang || "text")}</span>` +
      `<button type="button" class="copy-btn" data-copy><svg><use href="#i-copy"/></svg>Copy</button></div>` +
      `<pre><code>${esc(code)}</code></pre></div>`;
  }

  const splitRow = (l) =>
    l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

  function table(lines, i) {
    const head = splitRow(lines[i]);
    const aligns = splitRow(lines[i + 1]).map((c) =>
      c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "");
    const cell = (tag, c, k) =>
      `<${tag}${aligns[k] ? ` style="text-align:${aligns[k]}"` : ""}>${inline(esc(c))}</${tag}>`;
    let html = "<thead><tr>" + head.map((c, k) => cell("th", c, k)).join("") + "</tr></thead><tbody>";
    i += 2;
    while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
      const row = splitRow(lines[i]);
      html += "<tr>" + head.map((_, k) => cell("td", row[k] || "", k)).join("") + "</tr>";
      i++;
    }
    return [`<div class="table-wrap"><table>${html}</tbody></table></div>`, i];
  }

  function itemText(t) {
    const task = t.match(/^\[([ xX])\]\s+(.*)$/);
    if (task) {
      return `<input type="checkbox" disabled${task[1] !== " " ? " checked" : ""}>` + inline(esc(task[2]));
    }
    return inline(esc(t));
  }

  function list(lines, start) {
    const base = indentOf(lines[start]);
    const ordered = /^\s*\d/.test(lines[start]);
    const items = [];
    let i = start;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) {
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        if (j < lines.length && indentOf(lines[j]) >= base && (isItem(lines[j]) || indentOf(lines[j]) > base)) {
          i = j;
          continue;
        }
        break;
      }
      const ind = indentOf(l);
      if (ind < base) break;
      const m = l.match(reItem);
      if (m && ind < base + 2) {
        items.push({ text: m[3], children: [] });
        i++;
      } else if (m && items.length) {
        const [html, next] = list(lines, i);
        items[items.length - 1].children.push(html);
        i = next;
      } else if (items.length && ind > base) {
        items[items.length - 1].text += " " + l.trim();
        i++;
      } else {
        break;
      }
    }
    const tag = ordered ? "ol" : "ul";
    const body = items.map((it) => `<li>${itemText(it.text)}${it.children.join("")}</li>`).join("");
    return [`<${tag}>${body}</${tag}>`, i];
  }

  function render(md) {
    const lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(reFence);
      if (fence) {
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
        i++;
        out.push(codeBlock(buf.join("\n"), fence[2]));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const h = line.match(reHeading);
      if (h) {
        const lv = h[1].length;
        out.push(`<h${lv}>${inline(esc(h[2]))}</h${lv}>`);
        i++;
        continue;
      }
      if (reHr.test(line)) { out.push("<hr>"); i++; continue; }
      if (reQuote.test(line)) {
        const buf = [];
        while (i < lines.length && reQuote.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push(`<blockquote>${render(buf.join("\n"))}</blockquote>`);
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && reTableSep.test(lines[i + 1])) {
        const [html, next] = table(lines, i);
        out.push(html);
        i = next;
        continue;
      }
      if (isItem(line)) {
        const [html, next] = list(lines, i);
        out.push(html);
        i = next;
        continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !(buf.length && isBlockStart(lines[i], lines[i + 1]))) {
        buf.push(lines[i++].trim());
      }
      out.push(`<p>${buf.map((b) => inline(esc(b))).join("<br>")}</p>`);
    }
    return out.join("");
  }

  window.renderMarkdown = render;
})();
