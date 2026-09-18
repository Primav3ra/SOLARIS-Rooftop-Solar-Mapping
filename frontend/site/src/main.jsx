import React, { Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Routes, Route } from 'react-router-dom';

import './styles/global.css';
import Shell from './components/Shell.jsx';
import Landing from './pages/Landing.jsx';

// Route-level code splitting. Explore pulls in MapLibre, Landing's hero pulls
// in three.js, and the content pages pull in neither -- which is the whole
// point of splitting rather than shipping one bundle. `npm run budget` asserts
// that the content chunks stay free of WebGL.
const Explore = lazy(() => import('./pages/Explore.jsx'));
const Data = lazy(() => import('./pages/Data.jsx'));
const Method = lazy(() => import('./pages/Method.jsx'));
const Validation = lazy(() => import('./pages/Validation.jsx'));
const Models = lazy(() => import('./pages/Models.jsx'));
const Limitations = lazy(() => import('./pages/Limitations.jsx'));
const About = lazy(() => import('./pages/About.jsx'));
const NotFound = lazy(() => import('./pages/NotFound.jsx'));

function Loading() {
  return (
    <div className="page" style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
      <span className="spinner" aria-hidden="true" />
      <span style={{ color: 'var(--text-dim)' }}>Loading…</span>
    </div>
  );
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <Shell>
        <Suspense fallback={<Loading />}>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/explore" element={<Explore />} />
            <Route path="/data" element={<Data />} />
            <Route path="/method" element={<Method />} />
            <Route path="/validation" element={<Validation />} />
            <Route path="/models" element={<Models />} />
            <Route path="/limitations" element={<Limitations />} />
            <Route path="/about" element={<About />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Suspense>
      </Shell>
    </BrowserRouter>
  </React.StrictMode>,
);
