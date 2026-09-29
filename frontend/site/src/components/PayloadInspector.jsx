import { useMemo, useState } from "react";

/**
 * The exact request and response behind the result on screen.
 *
 * Every figure on this page is derived from one JSON response, and this panel
 * shows it -- plus the request that produced it and the equivalent curl
 * command. Two readers want this. A technical reviewer checking a number
 * against its source can see it without opening developer tools. And anyone
 * can reproduce the result outside the browser, which is the stronger claim:
 * the page is not doing anything the API does not.
 *
 * It re-renders from props, so it always matches the current result rather
 * than the first one computed.
 *
 * A native <details> element, so it opens and closes with no script and is
 * announced correctly by assistive technology.
 */
export default function PayloadInspector({
  endpoint = "/api/yield",
  request,
  response,
  meta,
}) {
  const [tab, setTab] = useState("response");

  const requestJson = useMemo(
    () => JSON.stringify(request ?? {}, null, 2),
    [request],
  );
  const responseJson = useMemo(
    () => JSON.stringify(response ?? {}, null, 2),
    [response],
  );
  const curl = useMemo(() => {
    const origin = typeof window === "undefined" ? "" : window.location.origin;
    return [
      `curl -X POST ${origin}${endpoint} \\`,
      `  -H 'content-type: application/json' \\`,
      `  -d '${JSON.stringify(request ?? {})}'`,
    ].join("\n");
  }, [endpoint, request]);

  if (!response) return null;

  const panes = {
    response: { label: "Response", text: responseJson, highlight: true },
    request: { label: "Request", text: requestJson, highlight: true },
    curl: { label: "curl", text: curl, highlight: false },
  };
  const active = panes[tab];
  const bytes = new Blob([responseJson]).size;

  return (
    <details className="payload">
      <summary className="payload__summary">
        <span>View the request and response behind this result</span>
        <span className="payload__meta">
          {endpoint} · {(bytes / 1024).toFixed(1)} KB
          {meta?.cache ? ` · cache ${meta.cache.toLowerCase()}` : ""}
          {meta?.requestId ? ` · ${meta.requestId}` : ""}
        </span>
      </summary>

      <div className="payload__body">
        <div className="payload__tabs" role="tablist" aria-label="Payload view">
          {Object.entries(panes).map(([key, pane]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              className={`payload__tab${tab === key ? " payload__tab--active" : ""}`}
              onClick={() => setTab(key)}
            >
              {pane.label}
            </button>
          ))}
          <CopyButton text={active.text} />
        </div>

        <pre
          className="payload__code"
          tabIndex={0}
          aria-label={`${active.label} payload`}
        >
          <code>
            {active.highlight ? highlightJson(active.text) : active.text}
          </code>
        </pre>
      </div>
    </details>
  );
}

function CopyButton({ text }) {
  const [state, setState] = useState("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("done");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState("idle"), 1600);
  };
  // The label states the outcome, so the confirmation is readable rather than
  // only a colour change.
  const label = { idle: "Copy", done: "Copied", failed: "Copy failed" }[state];
  return (
    <button
      type="button"
      className="payload__copy"
      onClick={copy}
      aria-live="polite"
    >
      {label}
    </button>
  );
}

/**
 * Minimal JSON highlighting: keys, strings, numbers, literals.
 *
 * A tokenising regex rather than a library, because the input is always our
 * own well-formed JSON.stringify output and a syntax-highlighting dependency
 * would outweigh the whole feature.
 */
function highlightJson(text) {
  const pattern =
    /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  const out = [];
  let last = 0;
  let match;
  let i = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    let cls = "json-number";
    if (token.startsWith('"')) cls = match[2] ? "json-key" : "json-string";
    else if (token === "true" || token === "false") cls = "json-bool";
    else if (token === "null") cls = "json-null";
    out.push(
      <span key={i++} className={cls}>
        {token}
      </span>,
    );
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
