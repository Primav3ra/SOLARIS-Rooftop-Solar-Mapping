import { useEffect, useId, useRef, useState } from "react";
import { glossaryEntry } from "../data/glossary.js";

/**
 * A technical term with its definition one hover -- or one Tab -- away.
 *
 *     <Term k="skill">skill over Erbs</Term>
 *
 * A real button, not a span with a title attribute: a `title` tooltip cannot
 * be reached from the keyboard, is not read reliably by screen readers, and
 * appears after a delay the reader cannot control. This opens on hover, on
 * focus and on tap, closes on Escape and on blur, and is linked to its trigger
 * by aria-describedby so assistive technology announces the definition with
 * the term.
 *
 * The popover flips below the term when there is no room above it, so a term
 * near the top of the viewport is never clipped by the navigation bar.
 */
export default function Term({ k, children }) {
  const entry = glossaryEntry(k);
  const [open, setOpen] = useState(false);
  const [below, setBelow] = useState(false);
  const id = useId();
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // Unknown key: render the text unchanged rather than a broken affordance.
  if (!entry) return <>{children}</>;

  const show = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    // 150px is roughly the popover's height plus the navigation bar.
    setBelow(Boolean(rect && rect.top < 150));
    setOpen(true);
  };

  return (
    <span className="term">
      <button
        ref={triggerRef}
        type="button"
        className="term__trigger"
        aria-describedby={id}
        aria-expanded={open}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onFocus={show}
        onBlur={() => setOpen(false)}
        onClick={() => (open ? setOpen(false) : show())}
      >
        {children ?? entry.term}
      </button>
      <span
        role="tooltip"
        id={id}
        className={`term__tip${below ? " term__tip--below" : ""}${open ? " term__tip--open" : ""}`}
      >
        <strong className="term__name">{entry.term}</strong>
        <span className="term__define">{entry.define}</span>
        {entry.why && <span className="term__why">{entry.why}</span>}
      </span>
    </span>
  );
}
