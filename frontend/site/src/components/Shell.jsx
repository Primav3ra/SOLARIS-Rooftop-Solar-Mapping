import { useEffect, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';

const LINKS = [
  { to: '/explore', label: 'Explore' },
  { to: '/data', label: 'Data' },
  { to: '/method', label: 'Method' },
  { to: '/validation', label: 'Validation' },
  { to: '/models', label: 'Models' },
  { to: '/limitations', label: 'Limitations' },
  { to: '/about', label: 'About' },
];

/** Persisted so a reader who prefers light mode is not fought on every visit. */
function useTheme() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem('solaris-theme') ?? 'dark',
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('solaris-theme', theme);
  }, [theme]);
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))];
}

export default function Shell({ children }) {
  const [theme, toggleTheme] = useTheme();
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();

  // Close the mobile menu and reset scroll on navigation. Without the scroll
  // reset, following a link from halfway down a long page lands halfway down
  // the next one.
  useEffect(() => {
    setOpen(false);
    window.scrollTo(0, 0);
  }, [pathname]);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <nav className="nav">
        <NavLink to="/" className="nav__brand">
          <svg className="nav__mark" viewBox="0 0 32 32" aria-hidden="true">
            <circle cx="16" cy="16" r="7" fill="var(--accent)" />
            <g stroke="var(--accent)" strokeWidth="2.4" strokeLinecap="round">
              <path d="M16 1v4M16 27v4M1 16h4M27 16h4" />
              <path d="M5.4 5.4l2.8 2.8M23.8 23.8l2.8 2.8M26.6 5.4l-2.8 2.8M8.2 23.8l-2.8 2.8" />
            </g>
          </svg>
          SOLARIS
        </NavLink>

        <button
          className="nav__toggle"
          aria-expanded={open}
          aria-controls="nav-links"
          onClick={() => setOpen((o) => !o)}
        >
          Menu
        </button>

        <div className="nav__links" id="nav-links" hidden={!open && window.innerWidth <= 900}>
          {LINKS.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              className={({ isActive }) => `nav__link${isActive ? ' is-active' : ''}`}
            >
              {link.label}
            </NavLink>
          ))}
          <button
            className="figure__toggle"
            onClick={toggleTheme}
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
            style={{ marginLeft: '0.4rem' }}
          >
            {theme === 'dark' ? 'Light' : 'Dark'}
          </button>
        </div>
      </nav>

      <main id="main">{children}</main>

      <footer className="footer">
        <div className="footer__inner">
          <div>
            <strong style={{ color: 'var(--text-dim)' }}>SOLARIS</strong> — rooftop solar
            potential for urban India, from open satellite data.
          </div>
          <div style={{ display: 'flex', gap: '1.25rem', flexWrap: 'wrap' }}>
            <NavLink to="/limitations">Limitations</NavLink>
            <NavLink to="/about">Licences &amp; attribution</NavLink>
            <a href="/docs" target="_blank" rel="noreferrer">
              API docs
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
